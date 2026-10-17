import hashlib
from pathlib import Path

import pytest
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.lineage import Node

from opslens.eval.provenance_models import LineageCase
from opslens.eval.provenance_runner import load_cases
from opslens.evidence.hashing import canonical_json, digest
from opslens.provenance import ProvenanceBuilder
from opslens.sql.policy import SQLPolicyConfig
from opslens.sql.schema import DatabaseSchemaSnapshot
from opslens.sql.service import GovernedSQLService

ROOT = Path(__file__).parents[2]
CASES = load_cases(ROOT / "tests/fixtures/provenance.jsonl", LineageCase)


@pytest.fixture
def setup(demo_db):
    policy = SQLPolicyConfig.load(ROOT / "data/sql_policy/default.v1.json")
    return (
        GovernedSQLService(demo_db, policy),
        ProvenanceBuilder(DatabaseSchemaSnapshot.load(demo_db, policy)),
    )


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.case_id)
def test_independent_ground_truth(setup, case):
    service, builder = setup
    execution = service.execute(case.sql)
    assert execution.status == "EXECUTED"
    result = builder.build(execution)
    assert result.lineage_status == case.expected_status
    assert result.source_tables == case.expected_source_tables
    actual = []
    for ordinal, output in enumerate(result.output_columns):
        assert output.output_ordinal == ordinal
        assert output.expression_sql
        actual.append(
            dict(
                output_name=output.output_name,
                source_columns=[c.canonical for c in output.source_columns],
                source_tables=output.source_tables,
                dependency_kind=output.dependency_kind,
                lineage_status=output.lineage_status,
            )
        )
    assert actual == [o.model_dump() for o in case.expected_outputs]
    contexts = [
        dict(kind=c.kind, source_columns=[r.canonical for r in c.source_columns], status=c.status)
        for c in result.context
    ]
    assert sorted(contexts, key=canonical_json) == sorted(
        [c.model_dump() for c in case.expected_context], key=canonical_json
    )
    assert result.query_fingerprint == execution.decision.query_fingerprint
    assert result.provenance_fingerprint == builder.build(execution).provenance_fingerprint
    assert result.provenance_fingerprint == digest(
        result.model_dump(mode="json", by_alias=True, exclude={"provenance_fingerprint"})
    )


def rewrite(result, sql, columns):
    decision = result.decision.model_copy(
        update={"effective_sql": sql, "query_fingerprint": hashlib.sha256(sql.encode()).hexdigest()}
    )
    return result.model_copy(update={"decision": decision, "columns": columns})


@pytest.mark.parametrize(
    "sql,columns,reason",
    [
        ("SELECT orders.no_such AS x FROM main.orders AS orders", ["x"], "UNKNOWN_LINEAGE_COLUMN"),
        ("SELECT z.region AS x FROM main.z AS z", ["x"], "UNKNOWN_LINEAGE_TABLE"),
        ("SELECT orders.region AS x FROM main.orders AS orders", ["y"], "RESULT_SCHEMA_MISMATCH"),
        (
            "SELECT orders.region AS x,orders.order_id AS y FROM main.orders AS orders",
            ["y", "x"],
            "RESULT_SCHEMA_MISMATCH",
        ),
        ("SELECT FROM (", ["x"], "LINEAGE_SQLGLOT_ERROR"),
        (
            "SELECT SUM(orders.order_id) OVER () AS x FROM main.orders AS orders",
            ["x"],
            "UNSUPPORTED_LINEAGE_STRUCTURE",
        ),
        (
            "SELECT (SELECT COUNT(*) FROM main.channels) AS x FROM main.orders",
            ["x"],
            "UNSUPPORTED_LINEAGE_STRUCTURE",
        ),
    ],
)
def test_untrusted_structure_never_guessed(setup, sql, columns, reason):
    service, builder = setup
    original = service.execute("SELECT region FROM orders")
    result = builder.build(rewrite(original, sql, columns))
    assert result.lineage_status == "UNRESOLVED"
    assert reason in result.reason_codes
    assert all(not o.source_columns for o in result.output_columns)


def test_fingerprint_mismatch(setup):
    service, builder = setup
    result = service.execute("SELECT region FROM orders")
    result = result.model_copy(
        update={"decision": result.decision.model_copy(update={"query_fingerprint": "0" * 64})}
    )
    with pytest.raises(ValueError, match="QUERY_FINGERPRINT_MISMATCH"):
        builder.build(result)


def test_unknown_leaf_and_sqlglot_error(setup, monkeypatch):
    import importlib

    module = importlib.import_module("opslens.provenance.lineage")
    service, builder = setup
    result = service.execute("SELECT region FROM orders")
    monkeypatch.setattr(
        module,
        "lineage",
        lambda *a, **kw: {
            "region": Node(name="region", expression=exp.Placeholder(), source=exp.Select())
        },
    )
    assert builder.build(result).reason_codes == ["UNRESOLVED_LINEAGE_NODE"]

    def broken(*a, **kw):
        raise SqlglotError("internal details")

    monkeypatch.setattr(module, "lineage", broken)
    assert builder.build(result).reason_codes == ["LINEAGE_SQLGLOT_ERROR"]


def test_physical_table_disagreement(setup):
    service, builder = setup
    result = service.execute("SELECT region FROM orders")
    analysis = result.decision.analysis.model_copy(update={"physical_tables": ["channels"]})
    result = result.model_copy(
        update={"decision": result.decision.model_copy(update={"analysis": analysis})}
    )
    trace = builder.build(result)
    assert trace.lineage_status == "PARTIAL"
    assert "PHYSICAL_TABLE_MISMATCH" in trace.reason_codes


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT region,COUNT(*) AS n FROM orders GROUP BY ROLLUP(region)",
        "SELECT COUNT(*) AS n FROM orders WHERE EXISTS(SELECT channel_id FROM channels)",
        "SELECT COUNT(*) AS n FROM orders WHERE order_id IN (SELECT order_id FROM refunds)",
        "WITH a AS (SELECT (SELECT COUNT(*) FROM channels) AS n FROM orders) SELECT n FROM a",
    ],
)
def test_complex_context_fails_closed(setup, sql):
    service, builder = setup
    result = service.execute(sql)
    assert result.status == "EXECUTED"
    trace = builder.build(result)
    assert trace.lineage_status == "UNRESOLVED"
    assert trace.reason_codes == ["UNSUPPORTED_LINEAGE_STRUCTURE"]


def test_scope_rowset_not_unrelated_outer_join(setup):
    service, builder = setup
    result = service.execute(
        "WITH a AS (SELECT COUNT(*) AS n FROM orders) "
        "SELECT a.n,c.channel_name FROM a CROSS JOIN channels c"
    )
    trace = builder.build(result)
    assert trace.lineage_status == "RESOLVED"
    assert trace.output_columns[0].dependency_kind == "ROWSET"
    assert trace.output_columns[0].source_tables == ["main.orders"]
    assert trace.output_columns[1].source_tables == ["main.channels"]

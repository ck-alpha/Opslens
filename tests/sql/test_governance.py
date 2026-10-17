import asyncio
import hashlib
import json
import threading
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest
from lumen.ai.tools.base import FunctionTool
from pydantic import ValidationError

from opslens.eval.sql_policy_models import load_sql_policy_cases
from opslens.eval.sql_policy_runner import run_sql_policy_benchmark
from opslens.integrations.lumen.sql import make_governed_sql_tool
from opslens.sql import GovernedSQLService, SQLPolicyConfig
from opslens.sql.executor import serialize_cell
from opslens.sql.schema import DatabaseSchemaSnapshot, readonly_connection

ROOT = Path(__file__).parents[2]
POLICY = ROOT / "data/sql_policy/default.v1.json"
CASES = ROOT / "tests/fixtures/sql_policy.jsonl"


@pytest.fixture
def policy():
    return SQLPolicyConfig.load(POLICY)


@pytest.fixture
def service(demo_db, policy):
    return GovernedSQLService(demo_db, policy)


@pytest.mark.parametrize("case", load_sql_policy_cases(CASES), ids=lambda c: c.id)
def test_adversarial_case(service, case):
    result = service.execute(case.sql)
    decision = result.decision
    assert decision.decision == case.expected_decision
    assert decision.primary_reason_code == case.expected_primary_reason_code
    assert result.status == ("EXECUTED" if case.expected_decision == "ALLOW" else "REJECTED")
    if case.expected_tables is not None:
        assert decision.analysis.physical_tables == sorted(case.expected_tables)
    assert result.row_count <= 1000


def test_policy_fingerprint(policy):
    data = json.loads(POLICY.read_text())
    for name in ("allowed_tables", "denied_columns", "forbidden_functions"):
        data[name].reverse()
    reordered = SQLPolicyConfig.model_validate_json(json.dumps(data, indent=4))
    assert reordered.fingerprint() == policy.fingerprint()
    data["max_result_rows"] = 500
    assert (
        SQLPolicyConfig.model_validate_json(json.dumps(data)).fingerprint() != policy.fingerprint()
    )
    with pytest.raises(ValidationError):
        policy.max_result_rows = 4
    assert isinstance(policy.allowed_tables, tuple)


@pytest.mark.parametrize(
    "field,value",
    [
        ("extra", 1),
        ("max_joins", 0),
        ("max_result_rows", -1),
        ("max_ctes", "6"),
        ("timeout_seconds", 0),
        ("timeout_seconds", float("inf")),
        ("version", "latest"),
        ("external_access", True),
        ("allowed_schemas", ["temp"]),
        ("allowed_tables", ["orders", "orders"]),
        ("allowed_tables", []),
        ("denied_columns", ["orders.customer_id", "orders.customer_id"]),
        ("forbidden_functions", ["UPPERCASE"]),
    ],
)
def test_invalid_policy(field, value):
    data = json.loads(POLICY.read_text())
    data[field] = value
    with pytest.raises(ValidationError):
        SQLPolicyConfig.model_validate_json(json.dumps(data))


@pytest.mark.parametrize(
    "field,value", [("allowed_tables", ["missing"]), ("denied_columns", ["orders.missing"])]
)
def test_schema_validation(demo_db, field, value):
    data = json.loads(POLICY.read_text())
    data[field] = value
    policy = SQLPolicyConfig.model_validate_json(json.dumps(data))
    with pytest.raises(ValueError):
        DatabaseSchemaSnapshot.load(demo_db, policy)


def test_views_are_not_allowed_tables(tmp_path, policy):
    db = tmp_path / "views.duckdb"
    with duckdb.connect(str(db)) as conn:
        conn.execute("CREATE VIEW orders AS SELECT 1 AS customer_id")
    with pytest.raises(ValueError):
        GovernedSQLService(db, policy)


def test_missing_database_fails_fast(tmp_path, policy):
    path = tmp_path / "missing.duckdb"
    with pytest.raises(FileNotFoundError):
        GovernedSQLService(path, policy)
    assert not path.exists()


def test_readonly_bypass_and_external_file_defense(tmp_path):
    db = tmp_path / "defense.duckdb"
    csv = tmp_path / "local.csv"
    csv.write_text("x\n123\n")
    with duckdb.connect(str(db)) as conn:
        conn.execute("CREATE TABLE t(x INTEGER); INSERT INTO t VALUES (1)")
    with readonly_connection(db) as conn:
        assert conn.execute("SELECT x FROM t").fetchall() == [(1,)]
        for sql in (
            "CREATE TABLE other(x INT)",
            "INSERT INTO t VALUES (2)",
            "UPDATE t SET x=3",
            "DELETE FROM t",
        ):
            with pytest.raises(duckdb.Error):
                conn.execute(sql)
        with pytest.raises(duckdb.Error):
            conn.execute("SELECT * FROM read_csv(?)", [str(csv)])
        settings = dict(
            conn.execute(
                "SELECT name, value FROM duckdb_settings() WHERE name IN "
                "('enable_external_access','autoload_known_extensions','autoinstall_known_extensions')"
            ).fetchall()
        )
        assert set(settings.values()) == {"false"}
        assert conn.execute("SELECT x FROM t").fetchall() == [(1,)]


def test_rejected_never_executes(service, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Rejected SQL reached executor")

    monkeypatch.setattr(service._executor, "_execute", forbidden)
    assert service.execute("DELETE FROM orders").status == "REJECTED"


def test_effective_sql_and_fingerprints(service):
    result = service.execute("SELECT order_id FROM orders")
    d = result.decision
    assert d.analysis.original_sql == "SELECT order_id FROM orders"
    assert d.analysis.normalized_sql == "SELECT order_id FROM orders"
    assert d.effective_sql.endswith("LIMIT 1000")
    assert d.query_fingerprint == hashlib.sha256(d.effective_sql.encode()).hexdigest()
    assert "RESULT_LIMIT_INJECTED" in d.warnings
    assert (
        service.evaluate(" SELECT order_id FROM orders; ").query_fingerprint == d.query_fingerprint
    )
    assert service.evaluate("SELECT order_id FROM orders LIMIT 20").warnings == []


def test_serialization_and_database_error(service):
    r = service.execute(
        "SELECT CAST(12.30 AS DECIMAL(12,2)) AS x, DATE '2026-08-01', "
        "TIMESTAMP '2026-08-01 12:30:00', NULL"
    )
    assert r.rows == [["12.30", "2026-08-01", "2026-08-01T12:30:00", None]]
    assert json.loads(r.model_dump_json())["rows"] == r.rows
    r = service.execute("SELECT CAST('bad' AS INTEGER)")
    assert r.status == "ERROR" and r.error_code == "DATABASE_ERROR"
    assert "/home/" not in r.model_dump_json()
    r = service.execute("SELECT CAST('NaN' AS DOUBLE)")
    assert r.status == "ERROR" and r.error_code == "SERIALIZATION_ERROR"
    assert serialize_cell({"x": [Decimal("0.10"), date(2026, 1, 1), datetime(2026, 1, 1)]}) == {
        "x": ["0.10", "2026-01-01", "2026-01-01T00:00:00"]
    }
    with pytest.raises(ValueError):
        serialize_cell(object())


def test_real_interrupt_and_followup(demo_db, policy):
    data = policy.model_dump_json()
    obj = json.loads(data)
    obj["timeout_seconds"] = 0.02
    service = GovernedSQLService(demo_db, SQLPolicyConfig.model_validate_json(json.dumps(obj)))
    # 480^3 potential combinations; actively interrupted, no range or external source.
    sql = (
        "SELECT SUM(LENGTH(a.order_id || b.order_id || c.order_id)) FROM orders a "
        "CROSS JOIN orders b CROSS JOIN orders c"
    )
    result = service.execute(sql)
    assert result.status == "TIMEOUT" and result.error_code == "QUERY_TIMEOUT"
    assert result.elapsed_ms < 5000
    assert not any(t.name == "opslens-sql-cancel" for t in threading.enumerate())
    # Normal production deadline on a fresh connection after interrupted work.
    assert (
        GovernedSQLService(demo_db, policy).execute("SELECT COUNT(*) FROM orders").status
        == "EXECUTED"
    )


def test_cancellation_cleanup_unit(service, monkeypatch):
    from opslens.sql import executor

    class SlowConnection:
        closed = False
        interrupted = threading.Event()

        def execute(self, sql):
            assert self.interrupted.wait(1), "Cancellation never arrived"
            raise duckdb.InterruptException("interrupted")

        def interrupt(self):
            self.interrupted.set()

        def close(self):
            self.closed = True

    connection = SlowConnection()
    monkeypatch.setattr(executor, "readonly_connection", lambda db: connection)
    result = service._executor._execute(service.evaluate("SELECT 1"), 0.01, 1000)
    assert result.status == "TIMEOUT" and connection.closed
    assert not any(t.name == "opslens-sql-cancel" for t in threading.enumerate())


def test_lumen_tool_no_llm(service, monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("LLM must not be invoked")

    monkeypatch.setattr(FunctionTool, "_invoke_prompt", forbidden)
    tool = make_governed_sql_tool(service)
    assert isinstance(tool, FunctionTool) and tool.render_output is False
    for sql, status in [
        ("SELECT COUNT(*) FROM orders", "EXECUTED"),
        ("DELETE FROM orders", "REJECTED"),
    ]:
        messages, context = asyncio.run(tool.respond([], {}, sql_query=sql))
        assert f'"status":"{status}"' in messages[0] and context == {}


def test_report_and_mutation_proof(demo_db):
    report = run_sql_policy_benchmark(demo_db, POLICY, CASES)
    assert report["cases"] >= 60 and report["passed"]
    assert report["database_unchanged"]
    assert report["database_fingerprints_before"] == report["database_fingerprints_after"]
    for name in [
        "decision_accuracy",
        "primary_reason_accuracy",
        "unsafe_query_block_rate",
        "safe_query_allow_rate",
        "allowed_execution_success_rate",
    ]:
        assert report[name] == 1.0
    assert report["unsafe_escape_rate"] == report["false_reject_rate"] == 0.0
    assert "/home/" not in json.dumps(report)


def test_cli_failure_is_nonzero(tmp_path, demo_db):
    import subprocess
    import sys

    data = [json.loads(line) for line in CASES.read_text().splitlines()[:2]]
    data[1]["expected_decision"] = "REJECT"
    data[1]["expected_primary_reason_code"] = "COLUMN_DENIED"
    cases = tmp_path / "wrong_expectations.jsonl"
    cases.write_text("".join(json.dumps(c) + "\n" for c in data))
    output = tmp_path / "report.json"
    process = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/run_sql_policy_benchmark.py"),
            "--db",
            str(demo_db),
            "--policy",
            str(POLICY),
            "--cases",
            str(cases),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert process.returncode == 1
    assert json.loads(output.read_text())["failures"] == ["S002"]


def test_ast_complexity_metadata(service):
    decision = service.evaluate(
        "WITH a AS (SELECT order_id FROM orders) "
        "SELECT a.order_id FROM a JOIN (SELECT order_id FROM refunds) r "
        "ON a.order_id=r.order_id LIMIT 10"
    )
    assert decision.decision == "ALLOW"
    a = decision.analysis
    assert (a.join_count, a.cte_count, a.subquery_count, a.table_reference_count) == (1, 1, 1, 3)
    assert a.top_level_limit == 10 and a.statement_count == 1
    assert a.schemas == ["main"] and a.physical_tables == ["orders", "refunds"]
    assert a.columns == ["orders.order_id", "refunds.order_id"]

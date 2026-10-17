import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import duckdb
import pytest

from opslens.evidence.builder import content_fingerprint
from opslens.evidence.hashing import result_hash
from opslens.evidence.models import ClaimBinding, MetricBinding
from opslens.knowledge import BusinessKnowledgeIndex
from opslens.provenance.snapshot import capture_snapshot
from opslens.sql.executor import serialize_cell


def test_execution_composition_and_no_evidence(service, monkeypatch):
    good = service.governed.execute("SELECT COUNT(*) AS n FROM orders")
    calls = []
    for status in ["REJECTED", "TIMEOUT", "ERROR", "EXECUTED"]:

        def fake(sql):
            calls.append(sql)
            return good.model_copy(update={"status": status})

        monkeypatch.setattr(service.governed, "execute", fake)
        result = service.execute_with_evidence("candidate")
        assert (result.evidence is not None) == (status == "EXECUTED")
    assert calls == ["candidate"] * 4


def test_actual_rejection(service):
    result = service.execute_with_evidence("DELETE FROM orders")
    assert result.execution.status == "REJECTED" and result.evidence is None


def test_result_hash_exact(service):
    result = service.governed.execute("SELECT order_id FROM orders ORDER BY order_id LIMIT 2")
    assert result_hash(result) == result_hash(result.model_copy(deep=True))
    for update in [
        {"rows": list(reversed(result.rows))},
        {"columns": ["different"]},
        {"rows": [["different"], result.rows[1]]},
    ]:
        assert result_hash(result) != result_hash(result.model_copy(update=update))
    value = Decimal("12345678901234567890.1234567890")
    assert serialize_cell(value) == "12345678901234567890.1234567890"
    exact = result.model_copy(
        update={"columns": ["n"], "row_count": 2, "rows": [[serialize_cell(value)], [None]]}
    )
    assert result_hash(exact) == result_hash(exact.model_copy(deep=True))
    with pytest.raises(ValueError):
        result_hash(result.model_copy(update={"row_count": 999}))


def test_snapshot_changes_and_privacy(demo_db, tmp_path):
    import shutil

    path = tmp_path / "copy.duckdb"
    shutil.copy2(demo_db, path)
    original = capture_snapshot(path, ["main.orders"])
    assert original == capture_snapshot(path, ["main.orders"])
    with duckdb.connect(str(path)) as con:
        con.execute("UPDATE _demo_metadata SET value='\"tomorrow\"' WHERE key='generated_at'")
    assert original == capture_snapshot(path, ["main.orders"])
    with duckdb.connect(str(path)) as con:
        con.execute(
            "UPDATE orders SET region='changed' WHERE order_id=(SELECT MIN(order_id) FROM orders)"
        )
    assert (
        original.snapshot_fingerprint
        != capture_snapshot(path, ["main.orders"]).snapshot_fingerprint
    )
    assert str(path) not in original.model_dump_json()
    with duckdb.connect(str(path)) as con:
        con.execute("DELETE FROM _demo_metadata WHERE key='seed'")
    with pytest.raises(ValueError, match="INCOMPLETE_DEMO_METADATA"):
        capture_snapshot(path, ["main.orders"])


@pytest.mark.parametrize("version,status", [("1.0", "ACTIVE"), ("0.9", "DEPRECATED")])
def test_exact_metric(service, version, status):
    record = service.execute_with_evidence(
        "SELECT COUNT(*) AS n FROM orders",
        metric_ref=MetricBinding(metric_id="gross_gmv", version=version, output_columns=["n"]),
    ).evidence
    assert record.metric.version == version and record.metric.status == status
    assert record.metric.binding_status == "DECLARED_NOT_VALIDATED"
    assert (
        record.validation_status == "NOT_EVALUATED"
    )  # Deliberately wrong semantic SQL still traceable.


@pytest.mark.parametrize(
    "mid,version,outputs,reason",
    [
        ("missing", "1.0", ["n"], "UNKNOWN_METRIC_VERSION"),
        ("gross_gmv", "99.0", ["n"], "UNKNOWN_METRIC_VERSION"),
        ("gross_gmv", "1.0", ["absent"], "INVALID_BINDING_OUTPUT"),
    ],
)
def test_invalid_metric(service, mid, version, outputs, reason):
    with pytest.raises(ValueError, match=reason):
        service.execute_with_evidence(
            "SELECT COUNT(*) AS n FROM orders",
            metric_ref=MetricBinding(metric_id=mid, version=version, output_columns=outputs),
        )


def test_claim(service):
    claim = ClaimBinding(claim_id="c", claim_text="未验证结论", output_columns=["n"])
    record = service.execute_with_evidence("SELECT COUNT(*) AS n FROM orders", claim=claim).evidence
    assert record.claim.binding_status == "LINKED_NOT_VALIDATED"
    with pytest.raises(ValueError, match="INVALID_BINDING_OUTPUT"):
        service.execute_with_evidence(
            "SELECT COUNT(*) AS n FROM orders",
            claim=claim.model_copy(update={"output_columns": ["missing"]}),
        )


def test_real_citation_identity(service):
    index = BusinessKnowledgeIndex(service.builder.corpus)
    asyncio.run(index.build())
    citations = [h.citation for h in asyncio.run(index.search("gross_gmv", top_k=2)).hits]
    record = service.execute_with_evidence(
        "SELECT COUNT(*) AS n FROM orders", citations=citations
    ).evidence
    assert [r.citation for r in record.knowledge] == citations
    for ref in record.knowledge:
        assert ref.corpus_fingerprint == index.corpus.fingerprint
        assert ref.citation.chunk_fingerprint in [c.chunk_fingerprint for c in citations]
        assert ref.document_content_sha256 in [d.content_sha256 for d in index.corpus.documents]
    for field, value in [("doc_id", "fake"), ("version", "99.0"), ("source_ref", "missing.md")]:
        fake = citations[0].model_copy(update={field: value})
        with pytest.raises(ValueError, match="INVALID_CITATION_IDENTITY"):
            service.execute_with_evidence("SELECT COUNT(*) AS n FROM orders", citations=[fake])


def test_record_time_and_content(service, record):
    service.builder.clock = lambda: record.recorded_at + timedelta(days=1)
    other = service.execute_with_evidence("SELECT COUNT(*) AS n FROM orders").evidence
    assert other.recorded_at != record.recorded_at
    assert other.content_fingerprint == record.content_fingerprint
    for clock in [
        lambda: datetime(2026, 1, 1),
        lambda: datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=8))),
    ]:
        service.builder.clock = clock
        with pytest.raises(ValueError, match="UTC_AWARE_TIMESTAMP_REQUIRED"):
            service.execute_with_evidence("SELECT COUNT(*) AS n FROM orders")
    data = json.loads(record.model_dump_json(by_alias=True))
    assert "rows" not in data["result"] and "home" not in data


@pytest.mark.parametrize(
    "field",
    ["result", "policy_fingerprint", "metric", "knowledge", "data_snapshot", "provenance", "claim"],
)
def test_content_changes(record, field):
    data = record.model_dump(mode="json", by_alias=True)
    original = content_fingerprint(record)
    if field == "policy_fingerprint":
        update = {"policy_fingerprint": "0" * 64}
    elif field == "result":
        update = {"result": record.result.model_copy(update={"result_hash": "0" * 64})}
    elif field == "data_snapshot":
        update = {field: record.data_snapshot.model_copy(update={"seed": 0})}
    elif field == "provenance":
        update = {field: record.provenance.model_copy(update={"reason_codes": ["x"]})}
    elif field == "metric":
        update = {field: {"version": "0.9"}}
    elif field == "knowledge":
        update = {field: [{"citation": "changed"}]}
    else:
        update = {field: {"claim_text": "changed"}}
    # Compare canonical payload directly for malformed inputs; production append revalidates models.
    from opslens.evidence.hashing import digest

    if field in ["metric", "knowledge", "claim"]:
        data.update(update)
        data.pop("recorded_at")
        data.pop("content_fingerprint")
        assert digest(data) != original
    else:
        assert content_fingerprint(record.model_copy(update=update)) != original


def test_partial_and_snapshot_error(service, monkeypatch):
    record = service.execute_with_evidence(
        "SELECT order_id AS x,channel_id AS x FROM orders"
    ).evidence
    assert record.completeness_status == "UNRESOLVED"
    import opslens.evidence.builder as module

    def broken(*a, **kw):
        raise ValueError("SNAPSHOT_FAILED")

    monkeypatch.setattr(module, "capture_snapshot", broken)
    with pytest.raises(ValueError, match="SNAPSHOT_FAILED"):
        service.execute_with_evidence("SELECT COUNT(*) AS n FROM orders")

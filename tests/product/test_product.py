import ast
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from opslens.observability.exporter import read_jsonl
from opslens.product.config import ProductConfig, ensure_demo, readiness
from opslens.product.events import ProductEvent, ProductEventRow
from opslens.product.metrics import summarize_events
from opslens.product.presenters import product_status, safe_report
from opslens.product.recipes import load_recipes
from opslens.runtime.engine import RuntimeEngine
from opslens.ui.state import SessionState


@pytest.mark.parametrize(
    "runtime,policy,validation,facts,expected",
    [
        ("PENDING", None, None, 0, "READY_TO_RUN"),
        ("RUNNING", None, None, 0, "RUNNING"),
        ("INTERRUPTED", None, None, 0, "RUNNING"),
        ("SUCCEEDED", "ALLOW", "PASS", 1, "VERIFIED"),
        ("SUCCEEDED", "ALLOW", "PASS", 0, "NEEDS_REVIEW"),
        ("SUCCEEDED", "ALLOW", "UNKNOWN", 1, "NEEDS_REVIEW"),
        ("FAILED", "REJECT", "FAIL", 0, "REJECTED"),
        ("FAILED", "ALLOW", "FAIL", 0, "FAILED"),
        ("NEEDS_REVIEW", "ALLOW", "UNKNOWN", 0, "NEEDS_REVIEW"),
        ("WAITING_INPUT", None, None, 0, "CLARIFICATION_REQUIRED"),
    ],
)
def test_state_mapping(runtime, policy, validation, facts, expected):
    assert (
        product_status(
            runtime, policy_decision=policy, validation_status=validation, fact_count=facts
        )
        == expected
    )


def test_clarification_status():
    assert product_status(action="ASK_USER") == "CLARIFICATION_REQUIRED"
    assert product_status(action="PROCEED") == "READY_TO_RUN"
    assert product_status(action="NEEDS_REVIEW") == "NEEDS_REVIEW"
    assert product_status("FAILED", reason_codes=("DIMENSION_NOT_ALLOWED",)) == "NEEDS_REVIEW"


def test_clarify_registry_citations(product_service):
    result = asyncio.run(product_service.clarify("8 月 GMV 是多少？"))
    assert result.action == "ASK_USER"
    assert {o.metric_id for o in result.options} == {"gross_gmv", "net_gmv"}
    for option in result.options:
        definition = product_service.registry.get(option.metric_id, option.version)
        assert option.description == definition.description
        assert option.citation.doc_id == f"metric:{option.metric_id}"
        assert option.citation.chunk_fingerprint


@pytest.mark.parametrize(
    "recipe_id,status",
    [
        ("gross_gmv_august", "VERIFIED"),
        ("net_gmv_august", "VERIFIED"),
        ("paid_orders_by_region", "VERIFIED"),
        ("refund_amount_rate_august", "VERIFIED"),
        ("policy_rejection_demo", "REJECTED"),
        ("known_limitation_demo", "NEEDS_REVIEW"),
    ],
)
def test_real_product_journeys(product_service, recipe_id, status):
    recipe = product_service.load_demo_recipe(recipe_id)
    view = product_service.start(recipe.runtime_request, idempotency_key=uuid4().hex)
    assert view.status == status, view.reason_codes
    assert view.has_verified_facts == (status == "VERIFIED")
    assert view.report and (bool(view.report.verified_facts) == view.has_verified_facts)
    assert view.reason_codes
    if status == "VERIFIED":
        assert view.report.overall_status == "PASS"
        assert view.evidence.completeness == "COMPLETE"
        assert view.observation.validation_status == "PASS"
    if recipe_id == "known_limitation_demo":
        assert "DIMENSION_NOT_ALLOWED" in view.reason_codes
        assert view.observation.final_status == "FAILED"  # preserve the persisted domain status
    if recipe_id == "policy_rejection_demo":
        assert view.observation.policy_decision == "REJECT"
    for kind in ("markdown", "report", "observation"):
        exported = product_service.export(view.run_id, kind)
        assert recipe.runtime_request.candidate_sql not in exported
        assert "request_payload" not in exported and "original_sql" not in exported
    assert read_jsonl(product_service.export(view.run_id, "observation"))[0] == view.observation


def test_wrong_semantic_sql_and_report_guard(product_service):
    request = product_service.load_demo_recipe("gross_gmv_august").runtime_request
    wrong = request.model_copy(
        update={
            "candidate_sql": request.candidate_sql.replace(
                "i.quantity * i.unit_price - i.discount_amount", "i.quantity * i.unit_price"
            )
        }
    )
    view = product_service.start(wrong, idempotency_key=uuid4().hex)
    assert view.status == "FAILED" and not view.has_verified_facts
    assert "METRIC_FORMULA_MISMATCH" in view.reason_codes
    good = product_service.start(request, idempotency_key=uuid4().hex)
    for status in ("FAILED", "REJECTED", "NEEDS_REVIEW"):
        assert not safe_report(good.report, status).verified_facts
    assert good.report.verified_facts  # projection never mutates source


def test_idempotency_rerun_and_recovery(product_service):
    service = product_service
    request = service.load_demo_recipe("gross_gmv_august").runtime_request
    key = uuid4().hex
    first = service.start(request, idempotency_key=key)
    assert service.start(request, idempotency_key=key).run_id == first.run_id
    assert first.can_rerun and not first.can_resume
    rerun = service.rerun(first.task_id)
    assert rerun.run_id != first.run_id and rerun.task_id == first.task_id
    assert rerun.observation.event_summary.reuse_count == 0

    class Stop(BaseException):
        pass

    def interrupt(point, step):
        if point == "after_checkpoint" and step.ordinal == 1:
            raise Stop()

    engine = RuntimeEngine(service.runtime.database, service.runtime.pipeline, hook=interrupt)
    _, run = engine.submit(uuid4().hex, request)
    with pytest.raises(Stop):
        engine.resume(run.run_id)
    assert service.get_run(run.run_id).can_resume
    resumed = service.resume(run.run_id)
    assert resumed.status == "VERIFIED"
    assert resumed.observation.event_summary.interruption_count == 1
    assert resumed.observation.event_summary.reuse_count == 1


def test_context_drift(product_service):
    service = product_service
    req = service.load_demo_recipe("gross_gmv_august").runtime_request
    _, run = service.runtime.submit(uuid4().hex, req)
    with service.runtime.database.begin() as connection:
        connection.execute(
            text("UPDATE runs SET runtime_context_fingerprint=:fp WHERE run_id=:id"),
            {"fp": "f" * 64, "id": run.run_id},
        )
    view = service.resume(run.run_id)
    assert view.status == "NEEDS_REVIEW"
    assert "RUNTIME_CONTEXT_DRIFT" in view.reason_codes


def test_bounded_ordered_history(product_service):
    service = product_service
    req = service.load_demo_recipe("gross_gmv_august").runtime_request
    ids = []
    for _ in range(105):
        _, run = service.runtime.submit(uuid4().hex, req)
        ids.append(run.run_id)
    runs = service.repository.list_recent_runs(100, "PENDING")
    assert len(runs) == 100 and runs[0].run_id == ids[-1]
    rows = service.history(2, "PENDING")
    assert len(rows) == 2 and rows[0]["run_id"] == ids[-1]
    assert set(rows[0]) == {
        "run_id",
        "task_id",
        "status",
        "metric_id",
        "metric_version",
        "created_at",
        "finished_at",
        "reason_codes",
    }
    for limit in (0, -1, 101, True):
        with pytest.raises(ValueError):
            service.history(limit)


def test_telemetry_privacy_append_only_and_failure(product_service, monkeypatch):
    service = product_service
    state = SessionState()
    for kind in (
        "METRIC_OPTION_SELECTED",
        "EVIDENCE_OPENED",
        "REPORT_EXPORTED",
        "FEEDBACK_HELPFUL",
        "FEEDBACK_NEEDS_CORRECTION",
        "FEEDBACK_UNCLEAR",
    ):
        assert service.record_event(state.fingerprint, kind, metric=("gross_gmv", "1.0"))
    with Session(service.runtime.database) as session:
        row = session.scalar(
            select(ProductEventRow).where(ProductEventRow.session_fingerprint == state.fingerprint)
        )
        event_id = row.event_id
        assert "question" not in row.__table__.columns
        assert state.nonce not in str(row.__dict__)
    for statement in (
        "UPDATE product_events SET metric_id='bad' WHERE event_id=:id",
        "DELETE FROM product_events WHERE event_id=:id",
    ):
        with pytest.raises(DBAPIError):
            with service.runtime.database.begin() as connection:
                connection.execute(text(statement), {"id": event_id})

    def broken(event):
        raise RuntimeError("password postgresql://private /home/private Traceback")

    monkeypatch.setattr(service.events, "append", broken)
    assert not service.record_event(state.fingerprint, "SESSION_STARTED")
    view = service.start(
        service.load_demo_recipe("gross_gmv_august").runtime_request, idempotency_key=uuid4().hex
    )
    assert view.status == "VERIFIED"


@pytest.mark.parametrize(
    "extra",
    [
        {"question": "x"},
        {"sql": "SELECT 1"},
        {"payload": {}},
        {"session_fingerprint": "email@example.com"},
        {"event_type": "free text"},
    ],
)
def test_closed_event_model(extra):
    with pytest.raises(ValidationError):
        ProductEvent.model_validate(
            {"session_fingerprint": "a" * 64, "event_type": "SESSION_STARTED", **extra}
        )


def test_windowed_metrics(product_service):
    end = datetime(2000, 1, 2, tzinfo=timezone.utc)
    result = summarize_events(product_service.events, end - timedelta(days=1), end)
    assert result["counts"] == {}
    assert result["report_export_rate"]["value"] is None
    for start in (end, end - timedelta(days=32), end.replace(tzinfo=None)):
        with pytest.raises(ValueError):
            summarize_events(product_service.events, start, end)


def test_recipe_and_import_isolation():
    assert len(load_recipes(Path("data/product/demo_recipes"))) >= 6
    for path in Path("data/product/demo_recipes").glob("*.json"):
        source = path.read_text()
        for field in ("reference_sql", "expected_behavior", "expected_result", "benchmark_case_id"):
            assert field not in source
    for folder in ("product", "ui"):
        for path in (Path("src/opslens") / folder).glob("*.py"):
            source = path.read_text()
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, ast.ImportFrom):
                    assert not (node.module or "").startswith("opslens.eval")
                if isinstance(node, ast.Import):
                    assert all(not a.name.startswith("opslens.eval") for a in node.names)
            assert "benchmarks/" not in source and "DuckDBMetricOracle" not in source
            assert "pn.pane.HTML" not in source


def test_readiness_lifecycle_and_safe_errors(product_service, tmp_path, monkeypatch):
    path = product_service.runtime.pipeline.validation.builder.db
    # Use the fixture's actual analysis path (no separate data source).
    config = ProductConfig(analysis_db=Path(path))
    ensure_demo(config)
    assert readiness(config)["ready"]
    missing = ProductConfig(analysis_db=tmp_path / "missing.duckdb", demo_mode=False)
    with pytest.raises(ValueError):
        ensure_demo(missing, generate=True)
    assert not missing.analysis_db.exists()
    monkeypatch.setenv(
        "OPSLENS_RUNTIME_DATABASE_URL", "postgresql+psycopg://user:password@localhost:1/private"
    )
    result = readiness(config)
    assert not result["ready"]
    for forbidden in ("password", "postgresql://", "Traceback", "/home/"):
        assert forbidden not in json.dumps(result)


def test_listener_and_session_security(monkeypatch):
    monkeypatch.setenv("OPSLENS_ALLOWED_WS_ORIGINS", "*")
    with pytest.raises(ValueError):
        ProductConfig.from_env()
    state = SessionState()
    a = state.submission_key("SELECT private")
    b = state.submission_key("SELECT private")
    assert a != b and len(a) == 64 and "SELECT" not in a


def test_demo_mismatch_does_not_rebuild(product_service, tmp_path, monkeypatch):
    import shutil

    import duckdb

    source = product_service.runtime.pipeline.validation.builder.db
    path = tmp_path / "changed.duckdb"
    shutil.copyfile(source, path)
    with duckdb.connect(str(path)) as connection:
        connection.execute(
            "UPDATE orders SET region='modified' WHERE order_id=(SELECT min(order_id) FROM orders)"
        )
    config = ProductConfig(analysis_db=path)
    with pytest.raises(ValueError, match="DEMO_FINGERPRINT_MISMATCH"):
        ensure_demo(config, generate=True)
    with duckdb.connect(str(path), read_only=True) as connection:
        assert (
            connection.execute("SELECT count(*) FROM orders WHERE region='modified'").fetchone()[0]
            == 1
        )
    # Missing demo creation uses exactly the prescribed config; matching data is then reused.
    fresh = ProductConfig(analysis_db=tmp_path / "new.duckdb")
    calls = []

    def generate(target, config):
        calls.append(config.seed)
        shutil.copyfile(source, target)

    monkeypatch.setattr("opslens.product.config.generate_demo_data", generate)
    ensure_demo(fresh, generate=True)
    ensure_demo(fresh, generate=True)
    assert calls == [20260910]
    with duckdb.connect(str(fresh.analysis_db)) as connection:
        connection.execute("UPDATE _demo_metadata SET value='false' WHERE key='synthetic'")
    with pytest.raises(ValueError, match="DEMO_METADATA_MISMATCH"):
        ensure_demo(fresh, generate=True)


def test_no_report_presentation_diagnostic_leak(product_service):
    from opslens.reporting.builder import interpretation

    request = product_service.load_demo_recipe("gross_gmv_august").runtime_request
    request = request.model_copy(
        update={
            "interpretations": [
                interpretation(
                    "CAUSAL",
                    "password postgresql://private /home/private <script>alert(1)</script>",
                )
            ]
        }
    )
    view = product_service.start(request, idempotency_key=uuid4().hex)
    assert not view.has_verified_facts and view.report.interpretations
    exported = product_service.export(view.run_id, "report")
    assert "password" not in exported and "postgresql://" not in exported
    assert all(not c.details for v in view.report.validation_summary for c in v.checks)

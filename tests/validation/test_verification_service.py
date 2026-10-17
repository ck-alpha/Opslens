"""Security boundaries, semantic contracts, and derivative report integrity."""

import ast
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from opslens.eval.validation_models import ValidationCase
from opslens.eval.validation_runner import ValidationHarness, run_validation_benchmark
from opslens.evidence.builder import content_fingerprint
from opslens.reporting.builder import interpretation, report_fingerprint
from opslens.reporting.markdown import render_markdown
from opslens.validation.intent import AnalysisIntent, TimeWindow
from opslens.validation.metric import results_agree
from opslens.validation.oracle import OracleResult
from opslens.validation.policy import ValidationPolicy
from opslens.validation.response import validate_response
from opslens.validation.service import ValidationService
from opslens.validation.status import aggregate, check, guarded, validation_fingerprint

ROOT = Path(__file__).parents[2]
CASES = [
    ValidationCase.model_validate_json(line)
    for line in (ROOT / "tests/fixtures/validation.jsonl").read_text().splitlines()
]


@pytest.fixture(scope="module")
def harness(demo_db):
    return ValidationHarness(demo_db, ROOT)


@pytest.mark.parametrize(
    "states,expected",
    [
        (["PASS", "PASS"], "PASS"),
        (["PASS", "FAIL"], "FAIL"),
        (["PASS", "UNKNOWN"], "UNKNOWN"),
        (["PASS", "NEEDS_REVIEW"], "NEEDS_REVIEW"),
        (["FAIL", "NEEDS_REVIEW"], "FAIL"),
        (["UNKNOWN", "NEEDS_REVIEW"], "NEEDS_REVIEW"),
    ],
)
def test_precedence(states, expected):
    assert aggregate([check("test", s) for s in states]) == expected


@pytest.mark.parametrize("flag", ["required", "applicable"])
def test_excluded_check(flag):
    assert aggregate([check("test", "FAIL", **{flag: False}), check("good")]) == "PASS"


def test_exception_redaction():
    def crash():
        raise RuntimeError("/home/private SECRET should never appear")

    outcome = guarded("METRIC", crash)
    assert outcome.status == "NEEDS_REVIEW"
    assert outcome.checks[0].details == {"error_type": "RuntimeError"}
    assert "SECRET" not in outcome.model_dump_json()
    assert guarded("METRIC", lambda: []).status == "NEEDS_REVIEW"


@pytest.mark.parametrize(
    "case", [c for c in CASES if c.category != "aggregation"], ids=lambda c: c.case_id
)
def test_end_to_end_cases(harness, case):
    bundle, intent, validation, report = harness.run(case)
    assert validation.overall_status == case.expected_status, validation.model_dump_json()
    assert case.expected_reason in validation.reason_codes
    if validation.overall_status != "PASS":
        assert not report.verified_facts
    if bundle.evidence:
        assert bundle.evidence.validation_status == "NOT_EVALUATED"


def test_full_benchmark(demo_db):
    result = run_validation_benchmark(
        demo_db,
        ROOT / "tests/fixtures/validation.jsonl",
        ROOT / "tests/fixtures/reports.jsonl",
        ROOT,
    )
    assert result["passed"], result["failures"]
    assert result["validation_cases"] >= 72 and result["report_cases"] >= 24


def test_fingerprints_and_rows(harness):
    case = next(c for c in CASES if c.case_id == "valid_gross_gmv")
    bundle, intent, record, report = harness.run(case)
    assert bundle.evidence
    before = bundle.evidence.model_dump_json(by_alias=True)
    assert (
        validation_fingerprint(
            record.model_copy(update={"evaluated_at": record.evaluated_at + timedelta(days=1)})
        )
        == record.validation_fingerprint
    )
    assert (
        report_fingerprint(
            report.model_copy(update={"generated_at": report.generated_at + timedelta(days=1)})
        )
        == report.report_fingerprint
    )
    assert validation_fingerprint(record.model_copy(update={"overall_status": "FAIL"})) != (
        record.validation_fingerprint
    )
    assert (
        report_fingerprint(report.model_copy(update={"title": "changed"}))
        != report.report_fingerprint
    )
    assert render_markdown(report) == render_markdown(report)
    assert all(
        title in render_markdown(report)
        for title in ["## Verified Facts", "## Interpretations", "## Uncertainty"]
    )
    assert before == bundle.evidence.model_dump_json(by_alias=True)
    assert content_fingerprint(bundle.evidence) == bundle.evidence.content_fingerprint
    result = validate_response(
        report, bundle, record, intent, case.mode, harness.registry, harness.validation_policy
    )
    assert aggregate(result) == "PASS"


def test_policy_strict_and_utc():
    with pytest.raises(ValidationError):
        ValidationPolicy(max_report_rows="20")
    with pytest.raises(ValidationError):
        ValidationPolicy(ratio_tolerance=float("nan"))
    with pytest.raises(ValidationError):
        ValidationPolicy(extra="forbidden")
    with pytest.raises(ValidationError):
        TimeWindow(start=datetime(2026, 9, 1), end=datetime(2026, 8, 1), timezone="Asia/Shanghai")
    window = TimeWindow(
        start=datetime(2026, 8, 1, tzinfo=timezone.utc),
        end=datetime(2026, 8, 2, tzinfo=timezone.utc),
        timezone="Asia/Shanghai",
    )
    assert window.local_bounds()[0] == datetime(2026, 8, 1, 8)


def test_semantic_result_comparison():
    oracle = OracleResult(columns=["region", "value"], rows=[["B", "2.00"], ["A", None]])
    assert results_agree(
        ["value", "region"], [[None, "A"], ["2.00", "B"]], oracle, "value", "CNY", 1e-9
    )
    assert not results_agree(
        ["region", "value"], [["B", "2.001"], ["A", None]], oracle, "value", "CNY", 1e-9
    )
    assert not results_agree(
        ["region", "value"], [["B", "2"], ["B", "2"]], oracle, "value", "CNY", 1e-9
    )
    ratio = OracleResult(columns=["value"], rows=[[0.5]])
    assert results_agree(["value"], [[0.5000000001]], ratio, "value", "RATIO", 1e-9)
    assert not results_agree(["value"], [[float("nan")]], ratio, "value", "RATIO", 1e-9)


def test_production_isolation():
    for directory in ["validation", "reporting"]:
        for path in (ROOT / "src/opslens" / directory).glob("*.py"):
            source = path.read_text()
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    assert not (node.module or "").startswith("opslens.eval")
            assert "benchmarks/" not in source and "reference_sql/" not in source
            for forbidden in ["chain_of_thought", "reasoning_trace", "hidden_reasoning"]:
                assert forbidden not in source
    source = (ROOT / "src/opslens/validation/oracle.py").read_text()
    assert "effective_sql" not in source and "GovernedSQLResult" not in source


def test_oracle_never_accepts_candidate(harness):
    with pytest.raises(TypeError):
        harness.oracle.evaluate(
            AnalysisIntent(
                metric_id="gross_gmv", metric_version="1.0", metric_output_column="value"
            ),
            "SELECT 1",
        )


@pytest.mark.parametrize("kind,status", [("CAUSAL", "NEEDS_REVIEW"), ("DESCRIPTIVE", "UNKNOWN")])
def test_interpretations_cannot_become_facts(harness, kind, status):
    bundle, intent, _, _ = harness.run(next(c for c in CASES if c.case_id == "valid_gross_gmv"))
    service = ValidationService(
        harness.builder, harness.policy, harness.validation_policy, harness.oracle
    )
    record, report = service.validate(
        bundle, intent, interpretations=[interpretation(kind, "social 渠道下降导致 GMV 下跌")]
    )
    assert record.overall_status == status
    assert not report.verified_facts
    assert report.uncertainties


def test_response_failure_is_fail_safe(harness):
    case = next(c for c in CASES if c.case_id == "crash_RESPONSE")
    _, _, record, report = harness.run(case)
    assert record.response.status == "NEEDS_REVIEW"
    assert not report.verified_facts


def test_row_truncation(harness):
    _, _, record, report = harness.run(next(c for c in CASES if c.case_id == "data_rows"))
    assert record.overall_status == "PASS"
    fact = report.verified_facts[0]
    assert fact.truncated and len(fact.rows) == 20 and fact.total_row_count > 20




def test_real_snapshot_drift(demo_db, tmp_path):
    import shutil

    import duckdb

    db = tmp_path / "drift.duckdb"
    shutil.copy2(demo_db, db)
    local = ValidationHarness(db, ROOT)
    case = next(c for c in CASES if c.case_id == "valid_gross_gmv")
    bundle, intent, _, _ = local.run(case)
    with duckdb.connect(str(db)) as con:
        con.execute(
            "UPDATE orders SET region='changed' WHERE order_id = (SELECT MIN(order_id) FROM orders)"
        )
    validation, report = ValidationService(
        local.builder, local.policy, local.validation_policy, local.oracle
    ).validate(bundle, intent)
    assert validation.overall_status == "NEEDS_REVIEW"
    assert "DATA_SNAPSHOT_DRIFT" in validation.evidence.reason_codes
    assert not report.verified_facts


def test_citation_integrity_and_corpus_drift(harness):
    import asyncio

    from opslens.knowledge import BusinessKnowledgeIndex

    index = BusinessKnowledgeIndex(harness.builder.corpus)
    asyncio.run(index.build())
    hit = asyncio.run(index.search("gross_gmv")).hits[0]
    bundle = harness.execution.execute_with_evidence(
        "SELECT COUNT(*) AS n FROM orders", citations=[hit.citation]
    )
    assert bundle.evidence
    service = ValidationService(
        harness.builder, harness.policy, harness.validation_policy, harness.oracle
    )
    assert service.validate(bundle, mode="DATA_QUERY")[0].overall_status == "PASS"
    ref = bundle.evidence.knowledge[0]
    bad = bundle.evidence.model_copy(
        update={"knowledge": [ref.model_copy(update={"document_content_sha256": "0" * 64})]}
    )
    bad = bad.model_copy(update={"content_fingerprint": content_fingerprint(bad)})
    result = service.validate(bundle.model_copy(update={"evidence": bad}), mode="DATA_QUERY")[0]
    assert result.evidence.status == "FAIL"
    assert "CITATION_DOCUMENT_HASH_MISMATCH" in result.reason_codes
    historical = bundle.evidence.model_copy(
        update={"knowledge": [ref.model_copy(update={"corpus_fingerprint": "historical"})]}
    )
    historical = historical.model_copy(
        update={"content_fingerprint": content_fingerprint(historical)}
    )
    result = service.validate(
        bundle.model_copy(update={"evidence": historical}), mode="DATA_QUERY"
    )[0]
    assert result.evidence.status == "NEEDS_REVIEW"
    assert "ARTIFACT_VERSION_DRIFT" in result.reason_codes


def test_response_rejects_result_from_different_execution(harness):
    case = next(c for c in CASES if c.case_id == "valid_gross_gmv")
    bundle, intent, record, report = harness.run(case)
    changed = bundle.model_copy(
        update={"execution": bundle.execution.model_copy(update={"rows": [["0.00"]]})}
    )
    from opslens.reporting.builder import build_report

    draft = build_report(
        changed, record, intent, case.mode, harness.registry, harness.validation_policy
    )
    checks = validate_response(
        draft, changed, record, intent, case.mode, harness.registry, harness.validation_policy
    )
    assert aggregate(checks) == "FAIL"
    assert "REPORT_VALUE_MISMATCH" in [c.reason_code for c in checks]


def test_benchmark_does_not_hide_wrong_expectations(demo_db, tmp_path):
    case = next(c for c in CASES if c.case_id == "valid_gross_gmv")
    # Retain source fixtures for report checks and alter one expectation.
    rows = [c.model_dump(mode="json") for c in CASES]
    rows[0]["expected_status"] = "FAIL"
    path = tmp_path / "wrong.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    result = run_validation_benchmark(
        demo_db, path, ROOT / "tests/fixtures/reports.jsonl", ROOT
    )
    assert not result["passed"]
    assert case.case_id in result["failures"]


def test_oracle_crash_and_unsupported_are_distinct(harness, monkeypatch):
    from opslens.validation.oracle import UnsupportedContract

    bundle, intent, _, _ = harness.run(next(c for c in CASES if c.case_id == "valid_gross_gmv"))
    service = ValidationService(
        harness.builder, harness.policy, harness.validation_policy, harness.oracle
    )

    def crash(_):
        raise RuntimeError("private error message")

    monkeypatch.setattr(harness.oracle, "evaluate", crash)
    record, report = service.validate(bundle, intent)
    assert record.metric.status == "NEEDS_REVIEW" and not report.verified_facts

    def unsupported(_):
        raise UnsupportedContract("unsupported")

    monkeypatch.setattr(harness.oracle, "evaluate", unsupported)
    record, report = service.validate(bundle, intent)
    assert record.metric.status == "UNKNOWN" and not report.verified_facts


def test_bound_filter_values(harness):
    from opslens.validation.intent import IntentFilter

    intent = AnalysisIntent(
        metric_id="gross_gmv",
        metric_version="1.0",
        metric_output_column="value",
        filters=[IntentFilter(column="orders.region", operator="EQ", value="x' OR TRUE --")],
    )
    result = harness.oracle.evaluate(intent)
    assert result.rows == [[None]]


def test_unsupported_contract_is_not_pass(harness):
    from opslens.validation.oracle import UnsupportedContract, formula

    definition = harness.registry.get("gross_gmv", "1.0")
    with pytest.raises(UnsupportedContract):
        formula(definition.model_copy(update={"expression": "MEDIAN(order_items.unit_price)"}))


def test_executed_requires_allow(harness):
    from opslens.validation.execution import validate_execution

    bundle, _, _, _ = harness.run(next(c for c in CASES if c.case_id == "valid_gross_gmv"))
    bad = bundle.execution.model_copy(
        update={"decision": bundle.execution.decision.model_copy(update={"decision": "REJECT"})}
    )
    assert aggregate(validate_execution(bad)) == "FAIL"

"""Deterministic report consistency, including adversarial drafts."""

from opslens.evidence.builder import content_fingerprint
from opslens.evidence.hashing import result_hash
from opslens.evidence.models import EvidenceExecutionResult
from opslens.metrics import MetricRegistry
from opslens.reporting.builder import build_report, interpretation, report_fingerprint
from opslens.reporting.models import InterpretationItem, VerifiedReport
from opslens.validation.intent import AnalysisIntent
from opslens.validation.models import ValidationCheck, ValidationMode, ValidationRecord
from opslens.validation.policy import ValidationPolicy
from opslens.validation.status import check, validation_fingerprint


def interpretation_checks(items: list[InterpretationItem]) -> list[ValidationCheck]:
    return [
        check(interpretation(i.kind, i.text).reason_code, interpretation(i.kind, i.text).status)
        for i in items
    ]


def validate_response(
    report: VerifiedReport,
    bundle: EvidenceExecutionResult,
    record: ValidationRecord,
    intent: AnalysisIntent | None,
    mode: ValidationMode,
    registry: MetricRegistry,
    policy: ValidationPolicy,
    interpretations: list[InterpretationItem] | None = None,
) -> list[ValidationCheck]:
    checks = []
    expected = build_report(bundle, record, intent, mode, registry, policy, interpretations)

    def require(ok: bool, code: str):
        if not ok:
            checks.append(check(code, "FAIL"))

    require(
        report.overall_status == record.overall_status
        and report.validation_summary == expected.validation_summary,
        "REPORT_STATUS_MISMATCH",
    )
    require(
        record.validation_fingerprint == validation_fingerprint(record)
        and report.validation_fingerprint == record.validation_fingerprint
        and report.evidence_content_fingerprint == record.evidence_content_fingerprint
        and report.report_fingerprint == report_fingerprint(report),
        "REPORT_FINGERPRINT_MISMATCH",
    )
    require(
        record.evidence_content_fingerprint
        == (bundle.evidence.content_fingerprint if bundle.evidence else None),
        "REPORT_FINGERPRINT_MISMATCH",
    )
    if report.verified_facts:
        require(
            bundle.execution.status == "EXECUTED" and bundle.evidence is not None,
            "UNVERIFIED_FACT_LEAK",
        )
        if bundle.evidence and bundle.execution.status == "EXECUTED":
            require(
                content_fingerprint(bundle.evidence) == bundle.evidence.content_fingerprint
                and result_hash(bundle.execution) == bundle.evidence.result.result_hash,
                "REPORT_VALUE_MISMATCH",
            )
    if record.overall_status != "PASS":
        require(not report.verified_facts, "UNVERIFIED_FACT_LEAK")
    require(report.uncertainties == expected.uncertainties, "UNCERTAINTY_NOT_REPORTED")
    require(report.interpretations == expected.interpretations, "REPORT_OUTPUT_MISMATCH")
    require(report.warnings == expected.warnings, "UNCERTAINTY_NOT_REPORTED")
    require(len(report.verified_facts) == len(expected.verified_facts), "REPORT_OUTPUT_MISMATCH")
    for actual, wanted in zip(report.verified_facts, expected.verified_facts):
        require(actual.rows == wanted.rows, "REPORT_VALUE_MISMATCH")
        require(
            actual.model_dump(exclude={"rows"}) == wanted.model_dump(exclude={"rows"}),
            "REPORT_OUTPUT_MISMATCH",
        )
    for item in report.interpretations:
        correct = interpretation(item.kind, item.text)
        require(
            item.status == correct.status and item.reason_code == correct.reason_code,
            "CAUSAL_CLAIM_REQUIRES_CAUSAL_EVIDENCE"
            if item.kind == "CAUSAL"
            else "FREE_TEXT_INTERPRETATION_NOT_VALIDATED",
        )
    return checks or [check("REPORT_VALID")]

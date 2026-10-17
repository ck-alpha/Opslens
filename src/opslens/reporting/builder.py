"""Build only authoritative structured facts; keep uncertainty visible."""

from datetime import datetime, timezone

from opslens.evidence.hashing import digest
from opslens.evidence.models import EvidenceExecutionResult
from opslens.metrics import MetricRegistry
from opslens.reporting.models import (
    InterpretationItem,
    UncertaintyItem,
    VerifiedFact,
    VerifiedReport,
)
from opslens.validation.intent import AnalysisIntent
from opslens.validation.models import ValidationMode, ValidationRecord
from opslens.validation.policy import ValidationPolicy


def report_fingerprint(report: VerifiedReport) -> str:
    return digest(report.model_dump(mode="json", exclude={"generated_at", "report_fingerprint"}))


def interpretation(kind: str, text: str) -> InterpretationItem:
    if kind not in ("CAUSAL", "DESCRIPTIVE"):
        raise ValueError("INVALID_INTERPRETATION_KIND")
    return InterpretationItem(
        kind=kind,
        text=text,
        status="NEEDS_REVIEW" if kind == "CAUSAL" else "UNKNOWN",
        reason_code="CAUSAL_CLAIM_REQUIRES_CAUSAL_EVIDENCE"
        if kind == "CAUSAL"
        else "FREE_TEXT_INTERPRETATION_NOT_VALIDATED",
    )


def build_report(
    bundle: EvidenceExecutionResult,
    validation: ValidationRecord,
    intent: AnalysisIntent | None,
    mode: ValidationMode,
    registry: MetricRegistry,
    policy: ValidationPolicy,
    interpretations: list[InterpretationItem] | None = None,
    generated_at: datetime | None = None,
) -> VerifiedReport:
    summaries = [validation.execution, validation.evidence, validation.metric, validation.response]
    uncertainties = [
        UncertaintyItem(validator=v.validator, reason_code=c.reason_code, summary=c.summary)
        for v in summaries
        for c in v.checks
        if c.required and c.applicable and c.status != "PASS"
    ]
    facts: list[VerifiedFact] = []
    definition = registry.get(intent.metric_id, intent.metric_version) if intent else None
    if validation.overall_status == "PASS":
        result = bundle.execution
        facts = [
            VerifiedFact(
                kind="VERIFIED_BUSINESS_METRIC"
                if mode == "METRIC_ANALYSIS"
                else "VERIFIED_DATA_RESULT",
                metric_id=intent.metric_id if intent and mode == "METRIC_ANALYSIS" else None,
                metric_version=intent.metric_version
                if intent and mode == "METRIC_ANALYSIS"
                else None,
                metric_output_column=intent.metric_output_column
                if intent and mode == "METRIC_ANALYSIS"
                else None,
                unit=definition.unit if definition and mode == "METRIC_ANALYSIS" else None,
                columns=result.columns,
                rows=result.rows[: policy.max_report_rows],
                total_row_count=result.row_count,
                truncated=result.row_count > policy.max_report_rows,
            )
        ]
    report = VerifiedReport(
        overall_status=validation.overall_status,
        validation_fingerprint=validation.validation_fingerprint,
        evidence_content_fingerprint=validation.evidence_content_fingerprint,
        verified_facts=facts,
        interpretations=interpretations or [],
        uncertainties=uncertainties,
        validation_summary=summaries,
        warnings=["DEPRECATED_METRIC"] if definition and definition.status == "DEPRECATED" else [],
        generated_at=generated_at or datetime.now(timezone.utc),
        report_fingerprint="",
    )
    return report.model_copy(update={"report_fingerprint": report_fingerprint(report)}, deep=True)

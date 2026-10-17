"""Compose governed SQL and evidence with independent validation and a checked report."""

from datetime import datetime, timezone

from opslens.evidence.builder import EvidenceBuilder
from opslens.evidence.hashing import digest
from opslens.evidence.ledger import EvidenceLedger
from opslens.evidence.models import EvidenceExecutionResult
from opslens.reporting.builder import build_report, interpretation
from opslens.reporting.models import InterpretationItem, VerifiedReport
from opslens.sql.policy import SQLPolicyConfig
from opslens.validation.evidence import validate_evidence
from opslens.validation.execution import validate_execution
from opslens.validation.intent import AnalysisIntent
from opslens.validation.metric import validate_metric
from opslens.validation.models import ValidationMode, ValidationRecord
from opslens.validation.oracle import MetricResultOracle
from opslens.validation.policy import ValidationPolicy
from opslens.validation.response import interpretation_checks, validate_response
from opslens.validation.status import aggregate, check, guarded, result, validation_fingerprint


class ValidationService:
    def __init__(
        self,
        builder: EvidenceBuilder,
        sql_policy: SQLPolicyConfig,
        policy: ValidationPolicy,
        oracle: MetricResultOracle | None,
    ):
        self.builder, self.sql_policy, self.policy, self.oracle = (
            builder,
            sql_policy,
            policy,
            oracle,
        )

    def validate(
        self,
        bundle: EvidenceExecutionResult,
        intent: AnalysisIntent | None = None,
        mode: ValidationMode = "METRIC_ANALYSIS",
        ledger: EvidenceLedger | None = None,
        interpretations: list[InterpretationItem] | None = None,
        evaluated_at: datetime | None = None,
    ) -> tuple[ValidationRecord, VerifiedReport]:
        if mode not in ("METRIC_ANALYSIS", "DATA_QUERY"):
            raise ValueError("INVALID_VALIDATION_MODE")
        items = [interpretation(i.kind, i.text) for i in interpretations or []]
        if bundle.evidence and bundle.evidence.claim:
            items.append(interpretation("DESCRIPTIVE", bundle.evidence.claim.claim_text))
        execution = guarded("EXECUTION", lambda: validate_execution(bundle.execution))
        evidence = guarded(
            "EVIDENCE",
            lambda: validate_evidence(
                bundle, self.builder, self.sql_policy, self.policy, mode, ledger
            ),
        )
        metric = guarded(
            "METRIC",
            lambda: validate_metric(bundle, intent, mode, self.builder, self.policy, self.oracle),
        )
        response = result("RESPONSE", interpretation_checks(items) + [check("REPORT_VALID")])
        record = ValidationRecord(
            evidence_content_fingerprint=bundle.evidence.content_fingerprint
            if bundle.evidence
            else None,
            validation_policy_id=self.policy.policy_id,
            validation_policy_version=self.policy.version,
            validation_policy_fingerprint=self.policy.fingerprint(),
            request_fingerprint=digest(
                {
                    "mode": mode,
                    "sql": bundle.execution.decision.analysis.original_sql,
                    "query_fingerprint": bundle.execution.decision.query_fingerprint,
                    "intent": intent.model_dump(mode="json") if intent else None,
                    "interpretations": [i.model_dump() for i in items],
                }
            ),
            execution=execution,
            evidence=evidence,
            metric=metric,
            response=response,
            overall_status=aggregate(
                c for v in [execution, evidence, metric, response] for c in v.checks
            ),
            reason_codes=sorted(
                {c.reason_code for v in [execution, evidence, metric, response] for c in v.checks}
            ),
            validation_fingerprint="",
            evaluated_at=evaluated_at or datetime.now(timezone.utc),
        )
        record = record.model_copy(
            update={"validation_fingerprint": validation_fingerprint(record)}
        )
        report = build_report(
            bundle, record, intent, mode, self.builder.registry, self.policy, items
        )
        checked = guarded(
            "RESPONSE",
            lambda: validate_response(
                report, bundle, record, intent, mode, self.builder.registry, self.policy, items
            ),
        )
        if checked.status != "PASS":
            response = result("RESPONSE", response.checks + checked.checks)
            record = record.model_copy(
                update={
                    "response": response,
                    "overall_status": aggregate(
                        c for v in [execution, evidence, metric, response] for c in v.checks
                    ),
                    "reason_codes": sorted(set(record.reason_codes + response.reason_codes)),
                }
            )
            record = record.model_copy(
                update={"validation_fingerprint": validation_fingerprint(record)}
            )
            report = build_report(
                bundle, record, intent, mode, self.builder.registry, self.policy, items
            )
        return record, report

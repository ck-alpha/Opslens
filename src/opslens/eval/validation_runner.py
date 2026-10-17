"""Offline adversarial evaluator, isolated from production validation."""

from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from opslens.eval.validation_models import ReportCase, ValidationCase
from opslens.evidence.builder import EvidenceBuilder, content_fingerprint
from opslens.evidence.hashing import digest
from opslens.evidence.ledger import EvidenceLedger
from opslens.evidence.models import EvidenceExecutionResult, MetricBinding
from opslens.evidence.service import EvidenceExecutionService
from opslens.knowledge import KnowledgeCorpus
from opslens.metrics import MetricRegistry
from opslens.reporting.builder import interpretation, report_fingerprint
from opslens.reporting.models import VerifiedReport
from opslens.sql.policy import SQLPolicyConfig
from opslens.sql.schema import DatabaseSchemaSnapshot
from opslens.sql.service import GovernedSQLService
from opslens.validation.intent import AnalysisIntent
from opslens.validation.models import ValidationRecord
from opslens.validation.oracle import DuckDBMetricOracle
from opslens.validation.policy import ValidationPolicy
from opslens.validation.response import validate_response
from opslens.validation.service import ValidationService
from opslens.validation.status import aggregate, check, guarded

CLOCK = datetime(2026, 9, 12, tzinfo=timezone.utc)


class ValidationHarness:
    def __init__(self, db: Path, root: Path):
        self.policy = SQLPolicyConfig.load(root / "data/sql_policy/default.v1.json")
        self.registry = MetricRegistry.from_directory(root / "data/metrics/definitions")
        self.builder = EvidenceBuilder(
            db,
            DatabaseSchemaSnapshot.load(db, self.policy),
            self.registry,
            KnowledgeCorpus(self.registry, root / "data/knowledge"),
            clock=lambda: CLOCK,
        )
        self.execution = EvidenceExecutionService(GovernedSQLService(db, self.policy), self.builder)
        self.validation_policy = ValidationPolicy.load(root / "data/validation/default.v1.json")
        self.oracle = DuckDBMetricOracle(db, self.registry)
        self.cache: dict[str, EvidenceExecutionResult] = {}

    def run(self, case: ValidationCase):
        intent = (
            AnalysisIntent(
                metric_id=case.metric_id,
                metric_version=case.metric_version,
                metric_output_column=case.output,
                time_window=case.time_window,
                dimensions=case.dimensions,
                filters=case.filters,
                time_grain=case.time_grain,
                allow_deprecated_metric=case.allow_deprecated,
            )
            if case.metric_id
            else None
        )
        binding = (
            MetricBinding(
                metric_id=intent.metric_id,
                version=intent.metric_version,
                output_columns=[case.output],
            )
            if intent
            else None
        )
        key = digest({"sql": case.sql, "binding": binding.model_dump() if binding else None})
        if key not in self.cache:
            self.cache[key] = self.execution.execute_with_evidence(case.sql, metric_ref=binding)
        bundle = self.cache[key].model_copy(deep=True)
        record = bundle.evidence
        action = case.action or ""
        ledger = None
        current_policy = self.policy
        if action.startswith("execution_"):
            state = action.removeprefix("execution_")
            bundle = bundle.model_copy(
                update={
                    "execution": bundle.execution.model_copy(update={"status": state}),
                    "evidence": record if state == "EXECUTED" else None,
                }
            )
        elif action == "missing":
            bundle = bundle.model_copy(update={"evidence": None})
        elif action in ("ledger_bad", "ledger_valid"):
            assert record
            ledger = EvidenceLedger()
            ledger.append(record)
            if action == "ledger_bad":
                ledger._entries[0] = ledger._entries[0].model_copy(update={"entry_hash": "bad"})
        elif action == "policy_drift":
            current_policy = self.policy.model_copy(update={"version": "9.9"})
        elif action in (
            "content",
            "query",
            "policy",
            "provenance",
            "result",
            "snapshot",
            "snapshot_drift",
            "registry_drift",
            "partial",
            "unresolved",
            "unknown_version",
            "identity",
        ):
            assert record
            if action == "content":
                record = record.model_copy(update={"task_id": "tampered"})
            elif action in ("query", "policy"):
                record = record.model_copy(update={action + "_fingerprint": "bad"})
            elif action == "provenance":
                record = record.model_copy(
                    update={
                        "provenance": record.provenance.model_copy(
                            update={"provenance_fingerprint": "bad"}
                        )
                    }
                )
            elif action == "result":
                record = record.model_copy(
                    update={"result": record.result.model_copy(update={"result_hash": "bad"})}
                )
            elif action in ("snapshot", "snapshot_drift"):
                snapshot = record.data_snapshot.model_copy(update={"snapshot_fingerprint": "bad"})
                if action == "snapshot_drift":
                    snapshot = snapshot.model_copy(update={"seed": 0})
                    snapshot = snapshot.model_copy(
                        update={
                            "snapshot_fingerprint": digest(
                                snapshot.model_dump(mode="json", exclude={"snapshot_fingerprint"})
                            )
                        }
                    )
                record = record.model_copy(update={"data_snapshot": snapshot})
            elif action == "registry_drift":
                assert record.metric
                record = record.model_copy(
                    update={
                        "metric": record.metric.model_copy(
                            update={"metric_registry_fingerprint": "historical"}
                        )
                    }
                )
            elif action in ("partial", "unresolved"):
                record = record.model_copy(update={"completeness_status": action.upper()})
            elif action == "unknown_version":
                assert intent and record.metric
                intent = intent.model_copy(update={"metric_version": "9.9"})
                record = record.model_copy(
                    update={"metric": record.metric.model_copy(update={"version": "9.9"})}
                )
            elif action == "identity":
                assert record.metric
                record = record.model_copy(
                    update={"metric": record.metric.model_copy(update={"metric_id": "net_gmv"})}
                )
            if action != "content":
                record = record.model_copy(
                    update={"content_fingerprint": content_fingerprint(record)}
                )
            bundle = bundle.model_copy(update={"evidence": record})
        service = ValidationService(
            self.builder,
            current_policy,
            self.validation_policy,
            None if action == "oracle_none" else self.oracle,
        )
        target = {
            "EXECUTION": "validate_execution",
            "EVIDENCE": "validate_evidence",
            "METRIC": "validate_metric",
            "RESPONSE": "validate_response",
        }
        context = (
            patch(
                "opslens.validation.service." + target[action.removeprefix("crash_")],
                side_effect=RuntimeError("boom"),
            )
            if action.startswith("crash_")
            else nullcontext()
        )
        with context:
            validation, report = service.validate(
                bundle, intent, case.mode, ledger, evaluated_at=CLOCK
            )
        # Stable report timestamps allow committed artifacts to reproduce exactly.
        report = report.model_copy(update={"generated_at": CLOCK})
        return bundle, intent, validation, report


def mutate_report(report: VerifiedReport, case: ReportCase, valid_report: VerifiedReport):
    action = case.action
    if action == "value":
        fact = report.verified_facts[0]
        report = report.model_copy(
            update={"verified_facts": [fact.model_copy(update={"rows": [[0]]})]}
        )
    elif action in ("version", "output"):
        field = "metric_version" if action == "version" else "metric_output_column"
        report = report.model_copy(
            update={
                "verified_facts": [report.verified_facts[0].model_copy(update={field: "wrong"})]
            }
        )
    elif action == "status":
        report = report.model_copy(update={"overall_status": "UNKNOWN"})
    elif action == "fingerprint":
        return report.model_copy(update={"report_fingerprint": "bad"})
    elif action == "leak":
        report = report.model_copy(update={"verified_facts": valid_report.verified_facts})
    elif action == "uncertainty":
        report = report.model_copy(update={"uncertainties": []})
    elif action == "causal":
        item = interpretation("CAUSAL", "social 渠道下降导致 GMV 下跌")
        report = report.model_copy(
            update={"interpretations": [item.model_copy(update={"status": "PASS"})]}
        )
    return report.model_copy(update={"report_fingerprint": report_fingerprint(report)})


def run_validation_benchmark(
    db: Path, validation_cases: Path, report_cases: Path, root: Path
) -> dict:
    cases = [
        ValidationCase.model_validate_json(line)
        for line in validation_cases.read_text().splitlines()
        if line.strip()
    ]
    reports = [
        ReportCase.model_validate_json(line)
        for line in report_cases.read_text().splitlines()
        if line.strip()
    ]
    if len({c.case_id for c in cases}) != len(cases) or len({c.case_id for c in reports}) != len(
        reports
    ):
        raise ValueError("DUPLICATE_CASE_ID")
    harness = ValidationHarness(db, root)
    observations = []
    failures = []
    outputs: dict[str, tuple] = {}
    scores: dict[str, list[bool]] = {}

    def score(name: str, value: bool):
        scores.setdefault(name, []).append(value)

    for case in cases:
        if case.category == "aggregation":
            actual = aggregate([check("AGGREGATION_PROBE", s) for s in case.states])
            reasons = ["AGGREGATION_PROBE"]
            validation = None
        else:
            bundle, intent, validation, report = harness.run(case)
            outputs[case.case_id] = (bundle, intent, validation, report, case.mode)
            actual, reasons = validation.overall_status, validation.reason_codes
            replay = harness.run(case)
            score(
                "validation_fingerprint_reproducibility",
                validation.validation_fingerprint == replay[2].validation_fingerprint,
            )
            score(
                "report_fingerprint_reproducibility",
                report.report_fingerprint == replay[3].report_fingerprint,
            )
            if case.category == "supported":
                score("supported_metric_pass_rate", validation.metric.status == "PASS")
                score("oracle_result_agreement_rate", "METRIC_ORACLE_MATCH" in reasons)
            if case.category == "m5_binding":
                score(
                    "m5_false_semantic_binding_catch_rate",
                    validation.metric.status != "PASS" and actual != "PASS",
                )
            if case.category == "integrity" and case.expected_status == "FAIL":
                score(
                    "evidence_integrity_detection_rate",
                    validation.evidence.status == "FAIL"
                    and case.expected_reason in validation.evidence.reason_codes,
                )
            if case.category == "crash":
                score("validator_fail_open_rate", actual == "PASS")
            if case.category == "execution":
                score(
                    "execution_validator_accuracy",
                    validation.execution.status == case.expected_status,
                )
            if case.category == "integrity":
                score(
                    "evidence_validator_accuracy",
                    validation.evidence.status == case.expected_status,
                )
            if case.category in ("supported", "semantic_negative", "m5_binding", "boundary"):
                score("metric_validator_accuracy", validation.metric.status == case.expected_status)
            if actual != "PASS":
                score("non_pass_business_fact_leakage_rate", bool(report.verified_facts))
            wanted_uncertainty = {
                (v.validator, c.reason_code)
                for v in [
                    validation.execution,
                    validation.evidence,
                    validation.metric,
                    validation.response,
                ]
                for c in v.checks
                if c.required and c.applicable and c.status != "PASS"
            }
            score(
                "uncertainty_coverage",
                wanted_uncertainty == {(u.validator, u.reason_code) for u in report.uncertainties},
            )
            if report.verified_facts:
                score(
                    "verified_fact_accuracy",
                    report.verified_facts[0].rows
                    == bundle.execution.rows[: harness.validation_policy.max_report_rows],
                )
        score("overall_status_accuracy", actual == case.expected_status)
        score("reason_code_accuracy", case.expected_reason in reasons)
        if case.expected_status != "PASS":
            score("semantic_false_pass_rate", actual == "PASS")
        if actual != case.expected_status or case.expected_reason not in reasons:
            failures.append(case.case_id)
        observations.append(
            {
                "case_id": case.case_id,
                "expected_status": case.expected_status,
                "actual_status": actual,
                "expected_reason": case.expected_reason,
                "reason_codes": reasons,
            }
        )
    for case in reports:
        bundle, intent, validation, report, mode = outputs[case.source]
        assert isinstance(validation, ValidationRecord)
        draft = mutate_report(report, case, outputs["valid_gross_gmv"][3])
        response = guarded(
            "RESPONSE",
            lambda validated=validation: validate_response(
                draft, bundle, validated, intent, mode, harness.registry, harness.validation_policy
            ),
        )
        ok = (
            response.status == case.expected_status
            and case.expected_reason in response.reason_codes
        )
        score("response_validator_accuracy", ok)
        if case.action == "causal":
            score("causal_claim_leakage_rate", response.status == "PASS")
        if not ok:
            failures.append(case.case_id)
        observations.append(
            {
                "case_id": case.case_id,
                "expected_status": case.expected_status,
                "actual_status": response.status,
                "expected_reason": case.expected_reason,
                "reason_codes": response.reason_codes,
            }
        )
    # Production handling of supplied causal/descriptive interpretations is also exercised.
    bundle, intent, _, _, mode = outputs["valid_gross_gmv"]
    for kind in ("CAUSAL", "DESCRIPTIVE"):
        record, report = ValidationService(
            harness.builder, harness.policy, harness.validation_policy, harness.oracle
        ).validate(bundle, intent, mode, interpretations=[interpretation(kind, "调用方解释")])
        score("causal_claim_leakage_rate", bool(report.verified_facts))
        score("uncertainty_coverage", bool(report.uncertainties))
    rates = {name: sum(values) / len(values) for name, values in scores.items()}
    for name, value in rates.items():
        wanted = (
            0.0 if name.endswith(("leakage_rate", "false_pass_rate", "fail_open_rate")) else 1.0
        )
        if value != wanted:
            failures.append("GATE:" + name)
    if len(cases) < 72 or len(reports) < 24:
        failures.append("INSUFFICIENT_CASES")
    return {
        "report_type": "validation_verified_report",
        "validation_version": "m6.0",
        "validation_policy_id": harness.validation_policy.policy_id,
        "validation_policy_version": harness.validation_policy.version,
        "validation_policy_fingerprint": harness.validation_policy.fingerprint(),
        "validation_cases": len(cases),
        "report_cases": len(reports),
        **rates,
        "metric_denominators": {k: len(v) for k, v in scores.items()},
        "observations": observations,
        "failures": failures,
        "passed": not failures,
    }

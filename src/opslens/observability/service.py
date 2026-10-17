"""Read-only observations over runtime repositories; no executor or telemetry writes."""

from collections import Counter
from datetime import datetime

from opslens.evidence.models import EvidenceRecord
from opslens.llm.models import GenerationMetadata
from opslens.observability.metrics import quantiles, summarize
from opslens.observability.models import (
    ArtifactSummary,
    EventSummary,
    RunObservation,
    StepObservation,
    TimingSummary,
    UsageSummary,
)
from opslens.observability.redaction import assert_safe
from opslens.persistence.repository import Repository
from opslens.reporting.models import VerifiedReport
from opslens.runtime.codec import decode
from opslens.runtime.failures import RuntimeFault
from opslens.runtime.state_machine import TERMINAL
from opslens.sql.models import GovernedSQLResult
from opslens.validation.models import ValidationRecord


def duration(start: datetime | None, end: datetime | None) -> float | None:
    value = (end - start).total_seconds() * 1000 if start and end else None
    quantiles([value])
    return value


class ObservabilityService:
    def __init__(self, repository: Repository):
        self.repository = repository

    def get_run(self, run_id: str) -> RunObservation:
        repo = self.repository
        run = repo.load_run(run_id)
        task = repo.load_task(run.task_id)
        steps = repo.list_steps(run_id)
        events = repo.load_events(run_id)
        artifacts = []
        for step in steps:
            stored = repo.load_step_artifacts(step.step_id)
            if any(a.step_id != step.step_id for a in stored):
                raise RuntimeFault("TRACE_CORRELATION_ERROR")
            if step.status == "SUCCEEDED":
                required = (
                    {"governed_sql_result"}
                    if step.ordinal == 1
                    else {"validation_record", "verified_report"}
                )
                if not required <= {a.artifact_type for a in stored}:
                    raise RuntimeFault("ARTIFACT_MISSING")
            artifacts.extend(stored)
        if len({a.artifact_type for a in artifacts}) != len(artifacts):
            raise RuntimeFault("ARTIFACT_SCHEMA_ERROR")
        step_ids = {s.step_id for s in steps}
        if (
            run.run_id != run_id
            or task.task_id != run.task_id
            or any(s.run_id != run_id for s in steps)
            or any(
                e.run_id != run_id or e.step_id is not None and e.step_id not in step_ids
                for e in events
            )
            or any(
                a.task_id != task.task_id or a.run_id != run_id or a.step_id not in step_ids
                for a in artifacts
            )
        ):
            raise RuntimeFault("TRACE_CORRELATION_ERROR")
        # An active run may change between repository reads; do not export a mixed snapshot.
        if repo.load_run(run_id) != run or repo.list_steps(run_id) != steps:
            raise RuntimeFault("OBSERVATION_SNAPSHOT_CHANGED")
        decoded = {
            a.artifact_type: decode(
                a.artifact_type, a.schema_version, a.payload, a.content_fingerprint
            )
            for a in artifacts
        }
        execution = decoded.get("governed_sql_result")
        evidence = decoded.get("evidence_record")
        validation = decoded.get("validation_record")
        report = decoded.get("verified_report")
        if isinstance(evidence, EvidenceRecord) and (
            evidence.task_id != task.task_id or evidence.run_id != run_id
        ):
            raise RuntimeFault("TRACE_CORRELATION_ERROR")
        reasons = {v for v in [run.failure_code] if v}
        reasons.update(s.last_error_code for s in steps if s.last_error_code)
        reasons.update(e.payload["reason_code"] for e in events if e.payload.get("reason_code"))
        for model in (evidence, validation):
            if isinstance(model, (EvidenceRecord, ValidationRecord)):
                reasons.update(model.reason_codes)
        if isinstance(execution, GovernedSQLResult):
            reasons.update(execution.decision.reason_codes)
            if execution.error_code:
                reasons.add(execution.error_code)
        counts = Counter(e.event_type for e in events)
        step_observations = [
            StepObservation(
                step_id=s.step_id,
                step_key=s.step_key,
                ordinal=s.ordinal,
                status=s.status,
                attempt_count=s.attempt_count,
                started_at=s.started_at,
                finished_at=s.finished_at,
                last_attempt_duration_ms=duration(s.started_at, s.finished_at),
                reused=any(
                    e.step_id == s.step_id and e.event_type == "STEP_REUSED" for e in events
                ),
                interrupted=any(
                    e.step_id == s.step_id and e.event_type == "STEP_INTERRUPTED" for e in events
                ),
                last_failure_class=s.last_failure_class,
                last_error_code=s.last_error_code,
                artifact_types=[a.artifact_type for a in artifacts if a.step_id == s.step_id],
                artifact_fingerprints=[
                    a.content_fingerprint for a in artifacts if a.step_id == s.step_id
                ],
            )
            for s in steps
        ]
        wall = duration(run.started_at, run.finished_at) if run.status in TERMINAL else None
        verified = len(report.verified_facts) if isinstance(report, VerifiedReport) else 0
        generation = [
            GenerationMetadata.model_validate(g)
            for g in task.request_payload.get("generation") or []
        ]
        usage = UsageSummary()
        if generation:

            def tokens(field):
                values = [getattr(g, field) for g in generation]
                return sum(values) if all(v is not None for v in values) else None

            usage = UsageSummary(
                accounting_mode="LOCAL_LLM",
                llm_calls=len(generation),
                input_tokens=tokens("prompt_tokens"),
                output_tokens=tokens("completion_tokens"),
            )
        # Runtime rows do not contain usage instrumentation. Do not infer zero from absence.
        observation = RunObservation(
            task_id=task.task_id,
            run_id=run_id,
            run_attempt=run.attempt_no,
            request_fingerprint=task.request_fingerprint,
            runtime_context_fingerprint=run.runtime_context_fingerprint,
            final_status=run.status,
            failure_class=run.failure_class,
            failure_code=run.failure_code,
            metric_id=evidence.metric.metric_id
            if isinstance(evidence, EvidenceRecord) and evidence.metric
            else None,
            metric_version=evidence.metric.version
            if isinstance(evidence, EvidenceRecord) and evidence.metric
            else None,
            query_fingerprint=execution.decision.query_fingerprint
            if isinstance(execution, GovernedSQLResult)
            else None,
            evidence_fingerprint=evidence.content_fingerprint
            if isinstance(evidence, EvidenceRecord)
            else None,
            validation_fingerprint=validation.validation_fingerprint
            if isinstance(validation, ValidationRecord)
            else None,
            report_fingerprint=report.report_fingerprint
            if isinstance(report, VerifiedReport)
            else None,
            validation_status=validation.overall_status
            if isinstance(validation, ValidationRecord)
            else None,
            policy_decision=execution.decision.decision
            if isinstance(execution, GovernedSQLResult)
            else None,
            reason_codes=sorted(reasons),
            document_ids=sorted({k.citation.doc_id for k in evidence.knowledge})
            if isinstance(evidence, EvidenceRecord)
            else [],
            verified_fact_count=verified,
            steps=step_observations,
            event_summary=EventSummary(
                event_count=len(events),
                event_types=dict(sorted(counts.items())),
                retry_count=counts["STEP_RETRY_SCHEDULED"],
                interruption_count=counts["RUN_INTERRUPTION_DETECTED"],
                step_interruption_count=counts["STEP_INTERRUPTED"],
                reuse_count=counts["STEP_REUSED"],
                terminal_event=next(
                    (
                        e.event_type
                        for e in reversed(events)
                        if e.event_type == "RUN_" + run.status and run.status in TERMINAL
                    ),
                    None,
                ),
            ),
            artifact_summary=ArtifactSummary(
                artifact_count=len(artifacts),
                artifact_types=dict(Counter(a.artifact_type for a in artifacts)),
                artifact_fingerprints=[a.content_fingerprint for a in artifacts],
            ),
            timings=TimingSummary(
                run_wall_clock_ms=wall,
                sql_elapsed_ms=execution.elapsed_ms
                if isinstance(execution, GovernedSQLResult)
                else None,
                validate_and_report_step_ms=next(
                    (
                        s.last_attempt_duration_ms
                        for s in step_observations
                        if s.step_key == "validate_and_report"
                    ),
                    None,
                ),
                time_to_verified_answer_ms=wall if run.status == "SUCCEEDED" and verified else None,
            ),
            usage=usage,
            generation=generation or None,
        )
        quantiles([observation.timings.sql_elapsed_ms])
        assert_safe(observation)
        return observation

    def summarize_runs(self, run_ids: list[str]):
        if len(run_ids) != len(set(run_ids)):
            raise ValueError("DUPLICATE_RUN_ID")
        return summarize([self.get_run(run_id) for run_id in run_ids])

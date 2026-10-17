"""Same-run recovery, checkpoint reuse, and explicit bounded runtime retry."""

import json
import time
from collections.abc import Callable
from typing import Any, Protocol

from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from opslens.evidence.hashing import digest
from opslens.evidence.models import EvidenceExecutionResult, EvidenceRecord
from opslens.persistence.repository import Repository
from opslens.reporting.models import VerifiedReport
from opslens.runtime.codec import decode, encode
from opslens.runtime.failures import RuntimeFailure, RuntimeFault, classify
from opslens.runtime.locking import claim_run
from opslens.runtime.models import RunView, StepView, TaskView
from opslens.runtime.policy import RuntimePolicy
from opslens.runtime.request import RuntimeAnalysisRequest
from opslens.runtime.state_machine import TERMINAL
from opslens.runtime.steps import Pipeline
from opslens.sql.models import GovernedSQLResult
from opslens.validation.models import ValidationRecord


class RuntimeLifecycleHook(Protocol):
    def __call__(self, point: str, step: StepView, /) -> None: ...


class NoOpHook:
    def __call__(self, point: str, step: StepView, /) -> None:
        pass


class RuntimeEngine:
    def __init__(
        self,
        database: Engine,
        pipeline: Pipeline,
        policy: RuntimePolicy | None = None,
        hook: RuntimeLifecycleHook | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.database, self.pipeline = database, pipeline
        self.policy = policy or RuntimePolicy()
        self.hook, self.sleeper = hook or NoOpHook(), sleeper
        self.repository = Repository(database)

    def submit(self, key: str, request: RuntimeAnalysisRequest) -> tuple[TaskView, RunView]:
        return self.repository.submit_task(
            key, request, self.policy, self.pipeline.context_fingerprint(self.policy)
        )

    def start(self, key: str, request: RuntimeAnalysisRequest) -> RunView:
        _, run = self.submit(key, request)
        return self.resume(run.run_id)

    def rerun(self, task_id: str) -> RunView:
        run = self.repository.create_new_run(
            task_id, self.policy, self.pipeline.context_fingerprint(self.policy)
        )
        return self.resume(run.run_id)

    def _load(self, repo: Repository, step: StepView) -> dict[str, Any]:
        artifacts = repo.load_step_artifacts(step.step_id)
        decoded: dict[str, Any] = {
            a.artifact_type: decode(
                a.artifact_type, a.schema_version, a.payload, a.content_fingerprint
            )
            for a in artifacts
        }
        required = (
            {"governed_sql_result"}
            if step.ordinal == 1
            else {"validation_record", "verified_report"}
        )
        if step.ordinal == 1 and isinstance(decoded.get("governed_sql_result"), GovernedSQLResult):
            if decoded["governed_sql_result"].status == "EXECUTED":
                required.add("evidence_record")
        if not required <= decoded.keys():
            raise RuntimeFault("ARTIFACT_MISSING_FOR_SUCCEEDED_STEP")
        if len(decoded) != len(artifacts) or decoded.keys() != required:
            raise RuntimeFault("ARTIFACT_SCHEMA_ERROR")
        if step.ordinal == 2:
            record, report = decoded["validation_record"], decoded["verified_report"]
            if (
                report.validation_fingerprint != record.validation_fingerprint
                or report.overall_status != record.overall_status
            ):
                raise RuntimeFault("ARTIFACT_INTEGRITY_ERROR")
        return decoded

    def _input(self, repo: Repository, step: StepView, request: RuntimeAnalysisRequest) -> str:
        upstream = {}
        if step.ordinal == 2:
            first = repo.list_steps(step.run_id)[0]
            upstream = {
                a.artifact_type: a.content_fingerprint
                for a in repo.load_step_artifacts(first.step_id)
            }
        return digest(
            dict(
                step_type=step.step_type,
                runtime_policy=self.policy.fingerprint(),
                request=request.fingerprint(),
                upstream=upstream,
            )
        )

    def _execute(
        self, repo: Repository, step: StepView, task: TaskView, request: RuntimeAnalysisRequest
    ) -> list[dict[str, Any]]:
        self.hook("before_execute", step)
        if step.ordinal == 1:
            bundle = self.pipeline.evidence.execute_with_evidence(
                request.candidate_sql,
                metric_ref=request.metric_ref,
                claim=request.claim,
                citations=request.citations,
                task_id=task.task_id,
                run_id=step.run_id,
            )
            artifacts = [encode("governed_sql_result", bundle.execution)]
            if bundle.evidence:
                artifacts.append(encode("evidence_record", bundle.evidence))
            return artifacts
        first = self._load(repo, repo.list_steps(step.run_id)[0])
        execution, evidence = first["governed_sql_result"], first.get("evidence_record")
        assert isinstance(execution, GovernedSQLResult)
        assert evidence is None or isinstance(evidence, EvidenceRecord)
        record, report = self.pipeline.validation.validate(
            EvidenceExecutionResult(execution=execution, evidence=evidence),
            request.intent,
            mode=request.validation_mode,
            interpretations=request.interpretations,
        )
        return [encode("validation_record", record), encode("verified_report", report)]

    def _step(
        self, repo: Repository, step: StepView, task: TaskView, request: RuntimeAnalysisRequest
    ) -> None:
        fingerprint = self._input(repo, step, request)
        if step.input_fingerprint and fingerprint != step.input_fingerprint:
            raise RuntimeFault("STEP_INPUT_FINGERPRINT_MISMATCH")
        if step.status == "SUCCEEDED":
            self._load(repo, step)
            repo.append_event(step.run_id, "STEP_REUSED", step.step_id, step_key=step.step_key)
            return
        if step.status == "FAILED" and step.last_failure_class != "RETRYABLE":
            raise _TerminalFailure(
                RuntimeFailure.model_validate(
                    {
                        "failure_class": step.last_failure_class or "NEEDS_REVIEW",
                        "error_code": step.last_error_code or "NONRETRYABLE_STEP_RESTART",
                    }
                )
            )
        while True:
            if step.attempt_count >= self.policy.max_step_attempts:
                # An interrupted last attempt consumed the budget too.
                failure = RuntimeFailure(
                    failure_class="NON_RETRYABLE", error_code="RETRY_EXHAUSTED"
                )
                if step.status == "INTERRUPTED":
                    repo.record_step_failure(step.step_id, failure)
                raise _TerminalFailure(failure)
            if step.status == "FAILED":
                repo.append_event(
                    step.run_id,
                    "STEP_RETRY_SCHEDULED",
                    step.step_id,
                    step_key=step.step_key,
                    attempt=step.attempt_count + 1,
                )
                self.sleeper(self.policy.retry_backoff_ms[step.attempt_count] / 1000)
            step = repo.begin_step(step.step_id, fingerprint)
            try:
                artifacts = self._execute(repo, step, task, request)
                self.hook("before_checkpoint", step)
            except Exception as exc:
                failure = classify(exc)
                repo.record_step_failure(step.step_id, failure)
                if failure.failure_class != "RETRYABLE":
                    raise _TerminalFailure(failure) from None
                step = repo.list_steps(step.run_id)[step.ordinal - 1]
                continue
            # Storage failures/unknown commit outcomes escape. Do not classify a
            # committed success as a failed execution; next resume inspects the DB.
            repo.commit_step_success(step.step_id, fingerprint, artifacts)
            self.hook("after_checkpoint", step)
            return

    def resume(self, run_id: str) -> RunView:
        with claim_run(self.database, run_id) as connection:
            repo = Repository(connection)
            run = repo.load_run(run_id)
            if run.status in TERMINAL:
                return run
            repo.begin_run(run_id)
            try:
                repo.load_events(run_id)
                if run.runtime_context_fingerprint != self.pipeline.context_fingerprint(
                    self.policy
                ):
                    raise RuntimeFault("RUNTIME_CONTEXT_DRIFT")
                task = repo.load_task(run.task_id)
                request = RuntimeAnalysisRequest.model_validate_json(
                    json.dumps(task.request_payload)
                )
                if request.fingerprint() != task.request_fingerprint:
                    raise RuntimeFault("REQUEST_INTEGRITY_ERROR")
                for step in repo.list_steps(run_id):
                    self._step(repo, step, task, request)
                final = self._load(repo, repo.list_steps(run_id)[1])
                record, report = final["validation_record"], final["verified_report"]
                assert isinstance(record, ValidationRecord) and isinstance(report, VerifiedReport)
                if record.overall_status == "PASS":
                    repo.finalize_run(run_id, "SUCCEEDED")
                else:
                    failed = record.overall_status == "FAIL"
                    repo.finalize_run(
                        run_id,
                        "FAILED" if failed else "NEEDS_REVIEW",
                        RuntimeFailure(
                            failure_class="NON_RETRYABLE" if failed else "NEEDS_REVIEW",
                            error_code="VALIDATION_FAILED"
                            if failed
                            else "VALIDATION_REQUIRES_REVIEW",
                        ),
                    )
            except DBAPIError:
                # Exit the held session. Never reconnect a runner after losing its lock.
                raise RuntimeFault("RUNTIME_STORAGE_UNAVAILABLE") from None
            except (_TerminalFailure, RuntimeFault) as exc:
                failure = exc.failure if isinstance(exc, _TerminalFailure) else classify(exc)
                status = {"NON_RETRYABLE": "FAILED", "NEEDS_USER_INPUT": "WAITING_INPUT"}.get(
                    failure.failure_class, "NEEDS_REVIEW"
                )
                repo.finalize_run(run_id, status, failure)
            return repo.load_run(run_id)


class _TerminalFailure(Exception):
    def __init__(self, failure: RuntimeFailure):
        self.failure = failure

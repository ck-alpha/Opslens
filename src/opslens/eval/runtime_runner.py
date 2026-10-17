"""PostgreSQL reliability scenarios with measured checks and real process crashes."""

import json
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from threading import Event as ThreadEvent
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from opslens.eval.runtime_models import RuntimeCase
from opslens.evidence.hashing import digest
from opslens.persistence.orm import Artifact, Run, Task
from opslens.persistence.repository import Repository
from opslens.runtime.codec import decode, encode, envelope_hash
from opslens.runtime.engine import RuntimeEngine
from opslens.runtime.failures import (
    NeedsUserInputRuntimeError,
    NonRetryableRuntimeError,
    RetryableRuntimeError,
    RuntimeFailure,
    RuntimeFault,
)
from opslens.runtime.locking import claim_run
from opslens.runtime.request import RuntimeAnalysisRequest
from opslens.runtime.state_machine import transition
from opslens.runtime.steps import Pipeline


class StopProcessSimulation(BaseException):
    """Same-process fixture interruption; actual process tests use os._exit."""


class Scenario:
    def __init__(self, database: Engine, analysis_db: Path, case: RuntimeCase):
        self.database, self.analysis_db, self.case = database, analysis_db, case
        self.repo = Repository(database)
        self.pipeline = Pipeline.build(analysis_db)
        self.runtime = RuntimeEngine(database, self.pipeline, sleeper=lambda _: None)
        self.request = RuntimeAnalysisRequest(candidate_sql="SELECT order_id FROM orders LIMIT 2")
        self.checks: dict[str, bool] = {}
        self.samples: dict[str, list[bool]] = {}
        self.run_id: str | None = None

    def check(self, name: str, result: bool) -> None:
        self.checks[name] = self.checks.get(name, True) and bool(result)
        self.samples.setdefault(name, []).append(bool(result))

    def submit(self) -> str:
        _, run = self.runtime.submit(str(uuid4()), self.request)
        self.run_id = run.run_id
        return run.run_id

    def pause(self, ordinal: int = 1, point: str = "after_checkpoint") -> str:
        run_id = self.submit()

        def hook(current: str, step: Any) -> None:
            if current == point and step.ordinal == ordinal:
                raise StopProcessSimulation

        try:
            RuntimeEngine(self.database, self.pipeline, hook=hook).resume(run_id)
        except StopProcessSimulation:
            pass
        else:
            raise AssertionError("fixture did not interrupt")
        return run_id

    def artifacts(self) -> list[Any]:
        assert self.run_id
        return [
            a
            for s in self.repo.list_steps(self.run_id)
            for a in self.repo.load_step_artifacts(s.step_id)
        ]

    def rejected(self, statement: Any, parameters: dict[str, Any] | None = None) -> bool:
        try:
            with self.database.begin() as connection:
                connection.execute(statement, parameters or {})
        except DBAPIError:
            return True
        return False

    def state(self) -> None:
        p = self.case.parameters
        try:
            actual = transition(p["kind"], p["old"], p["new"], new_run=p.get("new_run", False))
            self.check("state_transition", p["allowed"] and actual == p["new"])
        except RuntimeFault:
            self.check("state_transition", not p["allowed"])

    def idempotency(self) -> None:
        mode = self.case.parameters["mode"]
        key = str(uuid4())
        task, run = self.runtime.submit(key, self.request)
        self.run_id = run.run_id
        if mode == "concurrent":
            key = str(uuid4())
            barrier = Barrier(2)

            def submit():
                barrier.wait(timeout=10)
                return self.runtime.submit(key, self.request)

            with ThreadPoolExecutor(2) as pool:
                results = list(pool.map(lambda _: submit(), range(2)))
            task, run = results[0]
            self.run_id = run.run_id
            self.check("request_idempotency_accuracy", results[0] == results[1])
        elif mode == "conflict":
            try:
                self.runtime.submit(key, RuntimeAnalysisRequest(candidate_sql="SELECT 2"))
            except RuntimeFault as exc:
                self.check(
                    "idempotency_conflict_detection_rate",
                    exc.code == "IDEMPOTENCY_KEY_REQUEST_CONFLICT",
                )
            else:
                self.check("idempotency_conflict_detection_rate", False)
            self.check("request_idempotency_accuracy", self.repo.load_task(task.task_id) == task)
        elif mode == "rerun":
            self.runtime.resume(run.run_id)
            second = self.runtime.rerun(task.task_id)
            self.run_id = second.run_id
            self.check(
                "request_idempotency_accuracy",
                second.attempt_no == 2
                and second.run_id != run.run_id
                and second.task_id == task.task_id,
            )
            self.check(
                "rerun_no_cross_run_cache",
                all(s.attempt_count == 1 for s in self.repo.list_steps(second.run_id)),
            )
            return
        elif mode == "active_rerun":
            try:
                self.runtime.rerun(task.task_id)
            except RuntimeFault as exc:
                self.check(
                    "request_idempotency_accuracy", exc.code == "RERUN_REQUIRES_TERMINAL_RUN"
                )
            else:
                self.check("request_idempotency_accuracy", False)
        else:
            request = self.request
            if mode == "json_order":
                request = RuntimeAnalysisRequest.model_validate_json(
                    json.dumps(dict(reversed(list(request.model_dump(mode="json").items()))))
                )
            if mode == "terminal":
                self.runtime.resume(run.run_id)
            again, same = self.runtime.submit(key, request)
            self.check(
                "request_idempotency_accuracy",
                again.task_id == task.task_id and same.run_id == run.run_id,
            )
        with Session(self.database) as session:
            count = session.scalar(
                select(func.count()).select_from(Task).where(Task.idempotency_key == key)
            )
            runs = session.scalar(
                select(func.count()).select_from(Run).where(Run.task_id == task.task_id)
            )
        self.check("request_idempotency_accuracy", count == runs == 1)

    def failure(self) -> None:
        mode = self.case.parameters["mode"]
        if mode in {"validation_fail", "rejected", "timeout", "unknown_validation"}:
            if mode == "rejected":
                self.request = RuntimeAnalysisRequest(candidate_sql="DELETE FROM orders")
            elif mode == "validation_fail":
                self.request = RuntimeAnalysisRequest.model_validate_json(
                    Path("data/runtime/wrong_metric_request.json").read_text()
                )
            elif mode == "unknown_validation":
                self.request = RuntimeAnalysisRequest(
                    candidate_sql="SELECT order_id FROM orders LIMIT 2",
                    validation_mode="METRIC_ANALYSIS",
                )
            else:
                original = self.pipeline.evidence.governed.execute

                def timeout(sql: str):
                    return original(sql).model_copy(
                        update={"status": "TIMEOUT", "rows": [], "columns": [], "row_count": 0}
                    )

                self.pipeline.evidence.governed.execute = timeout
            self.run_id = self.runtime.start(str(uuid4()), self.request).run_id
            self.check(
                "nonretryable_auto_retry_rate",
                all(s.attempt_count == 1 for s in self.repo.list_steps(self.run_id)),
            )
            if mode in {"timeout", "unknown_validation"}:
                self.check(
                    "needs_review_preservation_rate",
                    self.repo.load_run(self.run_id).status == "NEEDS_REVIEW",
                )
            return
        exceptions = {
            "retry": RetryableRuntimeError,
            "exhausted": RetryableRuntimeError,
            "nonretryable": NonRetryableRuntimeError,
            "input": NeedsUserInputRuntimeError,
            "unexpected": RuntimeError,
            "validator_exception": RuntimeError,
        }
        target = 2 if mode == "validator_exception" else 1

        def hook(point: str, step: Any) -> None:
            if point == "before_execute" and step.ordinal == target:
                if mode != "retry" or step.attempt_count == 1:
                    raise exceptions[mode]("INJECTED_FAILURE")

        self.run_id = (
            RuntimeEngine(self.database, self.pipeline, hook=hook, sleeper=lambda _: None)
            .start(str(uuid4()), self.request)
            .run_id
        )
        run = self.repo.load_run(self.run_id)
        steps = self.repo.list_steps(self.run_id)
        if mode == "retry":
            self.check(
                "retryable_recovery_rate", run.status == "SUCCEEDED" and steps[0].attempt_count == 2
            )
        elif mode == "exhausted":
            self.check(
                "retry_exhaustion_accuracy",
                run.failure_code == "RETRY_EXHAUSTED" and steps[0].attempt_count == 3,
            )
        else:
            self.check("nonretryable_auto_retry_rate", steps[target - 1].attempt_count == 1)
            if mode in {"unexpected", "validator_exception"}:
                self.check(
                    "needs_review_preservation_rate",
                    run.status == "NEEDS_REVIEW"
                    and run.failure_code == "UNEXPECTED_STEP_EXCEPTION",
                )

    def integrity(self) -> None:
        mode = self.case.parameters["mode"]
        run_id = self.pause(2 if mode in {"validation_hash", "report_hash"} else 1)
        steps = self.repo.list_steps(run_id)
        if mode.startswith("append_"):
            table, operation = mode.removeprefix("append_").rsplit("_", 1)
            statement = (
                f"UPDATE {table} SET payload = payload WHERE run_id = :id"
                if operation == "update"
                else f"DELETE FROM {table} WHERE run_id = :id"
            )
            self.check(
                "append_only_enforcement_rate", self.rejected(text(statement), {"id": run_id})
            )
            return
        if mode.startswith("identity_"):
            field = mode.removeprefix("identity_")
            value = "'{}'::jsonb" if field == "request_payload" else "'changed'"
            self.check(
                "append_only_enforcement_rate",
                self.rejected(
                    text(f"UPDATE tasks SET {field} = {value} WHERE task_id = :id"),
                    {"id": self.repo.load_run(run_id).task_id},
                ),
            )
            return
        if mode in {"duplicate_commit", "conflicting_commit"}:
            artifacts = self.repo.load_step_artifacts(steps[0].step_id)
            encoded = [
                {
                    k: getattr(a, k)
                    for k in ("artifact_type", "schema_version", "payload", "content_fingerprint")
                }
                for a in artifacts
            ]
            if mode == "conflicting_commit":
                encoded[1]["payload"]["elapsed_ms"] = 999.0
                a = encoded[1]
                a["content_fingerprint"] = envelope_hash(
                    a["artifact_type"], a["schema_version"], a["payload"]
                )
            try:
                self.repo.commit_step_success(
                    steps[0].step_id, steps[0].input_fingerprint or "", encoded
                )
                self.check("checkpoint_idempotency", mode == "duplicate_commit")
            except RuntimeFault as exc:
                self.check(
                    "checkpoint_idempotency",
                    mode == "conflicting_commit" and exc.code == "ARTIFACT_IDEMPOTENCY_CONFLICT",
                )
            self.check("checkpoint_artifact_count", len(self.artifacts()) == 2)
            return
        if mode == "roundtrip":
            self.runtime.resume(run_id)
            for artifact in self.artifacts():
                result = decode(
                    artifact.artifact_type,
                    artifact.schema_version,
                    artifact.payload,
                    artifact.content_fingerprint,
                )
                self.check(
                    "artifact_roundtrip_accuracy",
                    encode(artifact.artifact_type, result)["payload"] == artifact.payload,
                )
            return
        # Corruption fixtures deliberately use the test DB owner's DDL privileges.
        # Application-level UPDATE/DELETE remains forbidden by triggers.
        artifacts = self.artifacts()
        target_type = {
            "evidence_hash": "evidence_record",
            "validation_hash": "validation_record",
            "report_hash": "verified_report",
        }.get(mode, "governed_sql_result")
        artifact = next(a for a in artifacts if a.artifact_type == target_type)
        with self.database.begin() as connection:
            connection.execute(text("ALTER TABLE runtime_artifacts DISABLE TRIGGER append_only"))
            if mode == "missing":
                connection.execute(
                    text("DELETE FROM runtime_artifacts WHERE artifact_id=:id"),
                    {"id": artifact.artifact_id},
                )
            elif mode == "step_input":
                connection.execute(
                    text("UPDATE steps SET input_fingerprint=:fp WHERE step_id=:id"),
                    {"fp": "0" * 64, "id": steps[0].step_id},
                )
            else:
                payload = dict(artifact.payload)
                if mode == "schema":
                    payload = {"invalid": True}
                elif mode in {"evidence_hash", "validation_hash", "report_hash"}:
                    field = {
                        "evidence_hash": "content_fingerprint",
                        "validation_hash": "validation_fingerprint",
                        "report_hash": "report_fingerprint",
                    }[mode]
                    payload[field] = "0" * 64
                else:
                    payload["row_count"] = 999
                fp = (
                    artifact.content_fingerprint
                    if mode == "envelope"
                    else envelope_hash(artifact.artifact_type, artifact.schema_version, payload)
                )
                connection.execute(
                    update(Artifact)
                    .where(Artifact.artifact_id == artifact.artifact_id)
                    .values(payload=payload, content_fingerprint=fp)
                )
            connection.execute(text("ALTER TABLE runtime_artifacts ENABLE TRIGGER append_only"))
        before = [s for s in self.repo.list_steps(run_id) if s.status == "SUCCEEDED"]
        run = self.runtime.resume(run_id)
        expected = {
            "missing": "ARTIFACT_MISSING_FOR_SUCCEEDED_STEP",
            "schema": "ARTIFACT_SCHEMA_ERROR",
            "step_input": "STEP_INPUT_FINGERPRINT_MISMATCH",
        }.get(mode, "ARTIFACT_INTEGRITY_ERROR")
        self.check("artifact_integrity_detection_rate", run.failure_code == expected)
        self.check("needs_review_preservation_rate", run.status == "NEEDS_REVIEW")
        after = {s.step_id: s for s in self.repo.list_steps(run_id)}
        for old in before:
            self.check(
                "successful_step_reexecution_rate",
                old.attempt_count == after[old.step_id].attempt_count,
            )

    def rollback(self) -> None:
        run_id = self.pause(point="before_checkpoint")
        step = self.repo.list_steps(run_id)[0]
        bundle = self.pipeline.evidence.execute_with_evidence(self.request.candidate_sql)
        assert bundle.evidence
        artifacts = [
            encode("governed_sql_result", bundle.execution),
            encode("evidence_record", bundle.evidence),
        ]

        def fail(index: int) -> None:
            if index == 0:
                raise RuntimeError("ROLLBACK_FIXTURE")

        try:
            self.repo.commit_step_success(
                step.step_id, step.input_fingerprint or "", artifacts, fail
            )
        except RuntimeError:
            pass
        self.check(
            "checkpoint_atomicity",
            not self.artifacts()
            and self.repo.list_steps(run_id)[0].status == "RUNNING"
            and "STEP_SUCCEEDED" not in [e.event_type for e in self.repo.load_events(run_id)],
        )
        self.runtime.resume(run_id)

    def context(self) -> None:
        run_id = self.pause()
        mode = self.case.parameters["mode"]
        policy = self.runtime.policy
        if mode == "runtime":
            policy = policy.model_copy(update={"version": "1.1"})
        elif mode == "validation":
            self.pipeline.validation.policy = self.pipeline.validation.policy.model_copy(
                update={"version": "1.1"}
            )
        elif mode == "sql":
            self.pipeline.validation.sql_policy = self.pipeline.validation.sql_policy.model_copy(
                update={"version": "1.1"}
            )
        elif mode == "corpus":
            self.pipeline.validation.builder.corpus.fingerprint = "0" * 64
        elif mode == "registry":
            registry = self.pipeline.validation.builder.registry
            original = registry.fingerprint
            registry.fingerprint = lambda: digest(original())
        run = RuntimeEngine(self.database, self.pipeline, policy).resume(run_id)
        self.check("context_drift_detection", run.failure_code == "RUNTIME_CONTEXT_DRIFT")
        self.check("needs_review_preservation_rate", run.status == "NEEDS_REVIEW")

    def process(self) -> None:
        mode = self.case.parameters["mode"]
        run_id = self.submit()
        with tempfile.TemporaryDirectory(prefix="opslens-m7-") as directory:
            counter = Path(directory) / "sql-count"
            ready = Path(directory) / "ready"
            command = [
                sys.executable,
                "-m",
                "opslens.eval.runtime_worker",
                "--analysis-db",
                str(self.analysis_db),
                "--run-id",
                run_id,
                "--counter",
                str(counter),
            ]
            ordinal = 2 if mode == "after_two" else 1
            point = "before_checkpoint" if mode == "before_one" else "after_checkpoint"
            if mode == "lock_crash":
                process = subprocess.Popen(
                    command + ["--point", "hold", "--ready", str(ready)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                try:
                    deadline = time.monotonic() + 25
                    while (
                        not ready.exists()
                        and process.poll() is None
                        and time.monotonic() < deadline
                    ):
                        time.sleep(0.02)
                    self.check("run_double_claim_rate", ready.exists())
                    before = self.repo.load_events(run_id)
                    try:
                        self.runtime.resume(run_id)
                    except RuntimeFault as exc:
                        self.check("run_double_claim_rate", exc.code == "RUN_LOCK_CONFLICT")
                    else:
                        self.check("run_double_claim_rate", False)
                    self.check("loser_no_mutation", before == self.repo.load_events(run_id))
                finally:
                    process.kill()
                    process.communicate(timeout=10)
            else:
                first = subprocess.run(
                    command + ["--point", point, "--ordinal", str(ordinal)],
                    capture_output=True,
                    timeout=35,
                )
                self.check("process_crash_exit", first.returncode == 73)
            before = self.repo.list_steps(run_id)
            after = subprocess.run(command, capture_output=True, timeout=35)
            self.check(
                "process_restart_recovery_rate",
                after.returncode == 0 and self.repo.load_run(run_id).status == "SUCCEEDED",
            )
            self.check("resume_success_rate", after.returncode == 0)
            final = self.repo.list_steps(run_id)
            for old, new in zip(before, final, strict=True):
                if old.status == "SUCCEEDED":
                    self.check(
                        "successful_step_reexecution_rate", old.attempt_count == new.attempt_count
                    )
            expected_sql = 2 if mode == "before_one" else 1
            self.check(
                "sql_invocation_count", len(counter.read_text().splitlines()) == expected_sql
            )
            if mode in {"before_one", "lock_crash"}:
                self.check("interrupted_step_recovery_rate", final[0].attempt_count == 2)
            if mode == "lock_crash":
                self.check("advisory_lock_recovery_rate", after.returncode == 0)

    def concurrency(self) -> None:
        mode = self.case.parameters["mode"]
        run_id = self.submit()
        if mode == "events":
            with ThreadPoolExecutor(4) as pool:
                list(pool.map(lambda _: self.repo.append_event(run_id, "AUDIT_CHECK"), range(12)))
            self.check("event_sequence_integrity_rate", len(self.repo.load_events(run_id)) == 14)
        elif mode == "claim":
            ready, release = ThreadEvent(), ThreadEvent()
            calls: list[int] = []

            def hook(point: str, step: Any) -> None:
                if point == "before_execute" and step.ordinal == 1:
                    calls.append(1)
                    ready.set()
                    if not release.wait(10):
                        raise AssertionError("concurrency timeout")

            with ThreadPoolExecutor(1) as pool:
                winner = pool.submit(
                    RuntimeEngine(self.database, self.pipeline, hook=hook).resume, run_id
                )
                try:
                    self.check("run_double_claim_rate", ready.wait(10))
                    before = self.repo.load_events(run_id)
                    try:
                        self.runtime.resume(run_id)
                    except RuntimeFault as exc:
                        self.check("run_double_claim_rate", exc.code == "RUN_LOCK_CONFLICT")
                    else:
                        self.check("run_double_claim_rate", False)
                    self.check("loser_no_mutation", self.repo.load_events(run_id) == before)
                finally:
                    release.set()
                self.check(
                    "run_double_claim_rate",
                    winner.result().status == "SUCCEEDED" and len(calls) == 1,
                )
        else:
            with claim_run(self.database, run_id):
                try:
                    with claim_run(self.database, run_id):
                        self.check("run_double_claim_rate", False)
                except RuntimeFault as exc:
                    self.check("run_double_claim_rate", exc.code == "RUN_LOCK_CONFLICT")
            with claim_run(self.database, run_id):
                self.check("advisory_lock_recovery_rate", True)

    def recovery(self) -> None:
        mode = self.case.parameters["mode"]
        run_id = self.submit()
        self.repo.begin_run(run_id)
        task = self.repo.load_task(self.repo.load_run(run_id).task_id)
        step = self.repo.list_steps(run_id)[0]
        fingerprint = self.runtime._input(self.repo, step, self.request)
        attempts = 3 if mode in {"failed_exhausted", "interrupted_exhausted"} else 1
        for attempt in range(attempts):
            step = self.repo.begin_step(step.step_id, fingerprint)
            if mode != "interrupted_exhausted" or attempt < attempts - 1:
                self.repo.record_step_failure(
                    step.step_id,
                    RuntimeFailure(
                        failure_class="NON_RETRYABLE"
                        if mode == "failed_nonretryable"
                        else "RETRYABLE",
                        error_code="INJECTED_FAILURE",
                    ),
                )
        run = self.runtime.resume(run_id)
        final = self.repo.list_steps(run_id)[0]
        if mode == "failed_retryable":
            self.check(
                "retryable_recovery_rate", run.status == "SUCCEEDED" and final.attempt_count == 2
            )
        elif mode == "failed_nonretryable":
            self.check("nonretryable_auto_retry_rate", final.attempt_count == 1)
        else:
            self.check(
                "retry_exhaustion_accuracy",
                run.failure_code == "RETRY_EXHAUSTED"
                and final.attempt_count == 3
                and final.status == "FAILED",
            )
        self.check("same_run_recovery", run.run_id == run_id and run.task_id == task.task_id)

    def terminal(self) -> None:
        run_id = self.submit()
        self.runtime.resume(run_id)
        before = self.repo.load_events(run_id)
        self.runtime.resume(run_id)
        self.check("terminal_resume_no_mutation", self.repo.load_events(run_id) == before)
        # Read-only artifact CLI takes no DuckDB dependency at all.
        completed = subprocess.run(
            [sys.executable, "scripts/runtime_cli.py", "artifacts", "--run-id", run_id],
            capture_output=True,
            timeout=20,
        )
        self.check(
            "durable_report_read",
            completed.returncode == 0 and b'"payload"' not in completed.stdout,
        )
        self.check("durable_artifact_count", len(json.loads(completed.stdout)) == 4)

    def run(self) -> dict[str, Any]:
        getattr(self, self.case.scenario)()
        observation: dict[str, Any] = {}
        if self.run_id:
            run = self.repo.load_run(self.run_id)
            task = self.repo.load_task(run.task_id)
            steps = self.repo.list_steps(self.run_id)
            events = self.repo.load_events(self.run_id)
            artifacts = self.artifacts()
            observation = dict(
                task_status=task.status,
                run_status=run.status,
                step_statuses=[s.status for s in steps],
                attempt_counts=[s.attempt_count for s in steps],
                event_types=[e.event_type for e in events],
                artifact_types=sorted(a.artifact_type for a in artifacts),
                reason_codes=[run.failure_code] if run.failure_code else [],
            )
            for name in ("task_status", "run_status", "step_statuses", "attempt_counts"):
                expected = getattr(self.case, f"expected_{name}")
                if expected:
                    metric = {
                        "task_status": "task_state_accuracy",
                        "run_status": "run_state_accuracy",
                        "step_statuses": "step_state_accuracy",
                        "attempt_counts": "attempt_accuracy",
                    }[name]
                    if isinstance(expected, list):
                        for actual, wanted in zip(observation[name], expected, strict=True):
                            self.check(metric, actual == wanted)
                    else:
                        self.check(metric, observation[name] == expected)
            for name in ("event_types", "reason_codes"):
                self.check(
                    name, set(getattr(self.case, f"expected_{name}")) <= set(observation[name])
                )
            for key in sorted({a.artifact_key for a in artifacts}):
                self.check(
                    "duplicate_artifact_rate", sum(a.artifact_key == key for a in artifacts) == 1
                )
            self.check(
                "event_sequence_integrity_rate",
                [e.sequence for e in events] == list(range(1, len(events) + 1)),
            )
        return dict(
            case_id=self.case.case_id,
            scenario=self.case.scenario,
            observation=observation,
            checks=self.checks,
            samples=self.samples,
            matched=bool(self.checks) and all(self.checks.values()),
        )

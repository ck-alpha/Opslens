"""Short atomic transactions with detached views and centralized transitions."""

from collections.abc import Callable
from typing import Any, TypeVar

from sqlalchemy import func, select
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from opslens.evidence.hashing import digest
from opslens.persistence.orm import Artifact, Event, Run, Step, Task, utcnow
from opslens.runtime.codec import decode
from opslens.runtime.failures import RuntimeFailure, RuntimeFault
from opslens.runtime.models import ArtifactView, EventView, RunView, StepView, TaskView
from opslens.runtime.policy import RuntimePolicy
from opslens.runtime.request import RuntimeAnalysisRequest
from opslens.runtime.state_machine import TERMINAL, transition
from opslens.sql.models import StrictModel

V = TypeVar("V", bound=StrictModel)
STEP_KEYS = ("execute_with_evidence", "validate_and_report")


def view(model: type[V], row: Any) -> V:
    if row is None:
        raise RuntimeFault("RUNTIME_ENTITY_NOT_FOUND")
    return model(**{name: getattr(row, name) for name in model.model_fields})


def event_hash(
    run_id: str, step_id: str | None, sequence: int, event_type: str, payload: dict[str, Any]
) -> str:
    return digest(
        dict(
            run_id=run_id,
            step_id=step_id,
            sequence=sequence,
            event_type=event_type,
            payload=payload,
        )
    )


class Repository:
    def __init__(self, bind: Engine | Connection):
        self.bind = bind

    def list_recent_runs(self, limit: int = 20, status: str | None = None) -> list[RunView]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("HISTORY_LIMIT_OUT_OF_RANGE")
        if status is not None and status not in {
            "PENDING", "RUNNING", "INTERRUPTED", "SUCCEEDED", "FAILED",
            "NEEDS_REVIEW", "WAITING_INPUT",
        }:
            raise ValueError("INVALID_HISTORY_STATUS")
        query = select(Run).order_by(Run.created_at.desc(), Run.run_id.desc()).limit(limit)
        if status:
            query = query.where(Run.status == status)
        with Session(self.bind) as session:
            return [view(RunView, row) for row in session.scalars(query)]

    def _event(
        self, session: Session, run_id: str, kind: str, step_id: str | None = None, **payload: Any
    ) -> None:
        session.execute(select(Run).where(Run.run_id == run_id).with_for_update()).scalar_one()
        sequence = (
            session.scalar(select(func.max(Event.sequence)).where(Event.run_id == run_id)) or 0
        ) + 1
        session.add(
            Event(
                run_id=run_id,
                step_id=step_id,
                sequence=sequence,
                event_type=kind,
                payload=payload,
                event_fingerprint=event_hash(run_id, step_id, sequence, kind, payload),
            )
        )
        session.flush()

    def append_event(
        self, run_id: str, kind: str, step_id: str | None = None, **payload: Any
    ) -> None:
        allowed = {
            "step_key",
            "attempt",
            "input_fingerprint",
            "artifact_keys",
            "artifact_fingerprints",
            "reason_code",
            "failure_class",
            "status",
            "task_id",
        }
        if set(payload) - allowed:
            raise RuntimeFault("UNSAFE_EVENT_PAYLOAD")
        with Session(self.bind) as session, session.begin():
            self._event(session, run_id, kind, step_id, **payload)

    def _new_run(
        self, session: Session, task: Task, attempt: int, policy: RuntimePolicy, context: str
    ) -> Run:
        run = Run(
            task_id=task.task_id,
            attempt_no=attempt,
            runtime_policy_id=policy.runtime_id,
            runtime_policy_version=policy.version,
            runtime_policy_fingerprint=policy.fingerprint(),
            runtime_context_fingerprint=context,
        )
        session.add(run)
        session.flush()
        for ordinal, key in enumerate(STEP_KEYS, 1):
            session.add(
                Step(run_id=run.run_id, step_key=key, step_type=key.upper(), ordinal=ordinal)
            )
        if attempt == 1:
            self._event(session, run.run_id, "TASK_CREATED", task_id=task.task_id)
        self._event(session, run.run_id, "RUN_CREATED")
        return run

    def submit_task(
        self, key: str, request: RuntimeAnalysisRequest, policy: RuntimePolicy, context: str
    ) -> tuple[TaskView, RunView]:
        if not key.strip():
            raise RuntimeFault("INVALID_IDEMPOTENCY_KEY")
        request = RuntimeAnalysisRequest.model_validate_json(request.model_dump_json())
        try:
            with Session(self.bind) as session, session.begin():
                task = Task(
                    idempotency_key=key,
                    request_fingerprint=request.fingerprint(),
                    request_payload=request.model_dump(mode="json"),
                )
                session.add(task)
                session.flush()  # UNIQUE serializes competing submissions.
                run = self._new_run(session, task, 1, policy, context)
                return view(TaskView, task), view(RunView, run)
        except IntegrityError:
            with Session(self.bind) as session:
                task = session.scalar(select(Task).where(Task.idempotency_key == key))
                if task is None:
                    raise RuntimeFault("TASK_SUBMISSION_CONSTRAINT_ERROR") from None
                if task.request_fingerprint != request.fingerprint():
                    raise RuntimeFault("IDEMPOTENCY_KEY_REQUEST_CONFLICT") from None
                run = session.scalar(
                    select(Run).where(Run.task_id == task.task_id).order_by(Run.attempt_no.desc())
                )
                return view(TaskView, task), view(RunView, run)

    def create_new_run(self, task_id: str, policy: RuntimePolicy, context: str) -> RunView:
        with Session(self.bind) as session, session.begin():
            task = session.execute(
                select(Task).where(Task.task_id == task_id).with_for_update()
            ).scalar_one()
            latest = (
                session.execute(
                    select(Run).where(Run.task_id == task_id).order_by(Run.attempt_no.desc())
                )
                .scalars()
                .first()
            )
            if latest is None or latest.status not in TERMINAL:
                raise RuntimeFault("RERUN_REQUIRES_TERMINAL_RUN")
            run = self._new_run(session, task, latest.attempt_no + 1, policy, context)
            task.status = transition("task", task.status, "RUNNING", new_run=True)
            task.updated_at = utcnow()
            return view(RunView, run)

    def load_task(self, task_id: str) -> TaskView:
        with Session(self.bind) as session:
            return view(TaskView, session.get(Task, task_id))

    def load_run(self, run_id: str) -> RunView:
        with Session(self.bind) as session:
            return view(RunView, session.get(Run, run_id))

    def list_steps(self, run_id: str) -> list[StepView]:
        with Session(self.bind) as session:
            return [
                view(StepView, row)
                for row in session.scalars(
                    select(Step).where(Step.run_id == run_id).order_by(Step.ordinal)
                )
            ]

    def load_events(self, run_id: str) -> list[EventView]:
        with Session(self.bind) as session:
            events = [
                view(EventView, row)
                for row in session.scalars(
                    select(Event).where(Event.run_id == run_id).order_by(Event.sequence)
                )
            ]
        for sequence, event in enumerate(events, 1):
            if event.sequence != sequence or event.event_fingerprint != event_hash(
                event.run_id, event.step_id, event.sequence, event.event_type, event.payload
            ):
                raise RuntimeFault("EVENT_INTEGRITY_ERROR")
        return events

    def load_step_artifacts(self, step_id: str) -> list[ArtifactView]:
        with Session(self.bind) as session:
            return [
                view(ArtifactView, row)
                for row in session.scalars(
                    select(Artifact)
                    .where(Artifact.step_id == step_id)
                    .order_by(Artifact.artifact_type)
                )
            ]

    def begin_run(self, run_id: str) -> None:
        with Session(self.bind) as session, session.begin():
            run = session.execute(
                select(Run).where(Run.run_id == run_id).with_for_update()
            ).scalar_one()
            task = session.get(Task, run.task_id)
            assert task is not None
            if run.status == "RUNNING":
                run.status = transition("run", run.status, "INTERRUPTED")
                self._event(session, run_id, "RUN_INTERRUPTION_DETECTED")
            resumed = run.status == "INTERRUPTED"
            run.status = transition("run", run.status, "RUNNING")
            run.started_at = run.started_at or utcnow()
            if task.status != "RUNNING":
                task.status = transition("task", task.status, "RUNNING")
            task.updated_at = utcnow()
            self._event(session, run_id, "RUN_RESUMED" if resumed else "RUN_STARTED")
            for step in session.scalars(select(Step).where(Step.run_id == run_id)):
                if step.status == "RUNNING":
                    step.status = transition("step", step.status, "INTERRUPTED")
                    self._event(
                        session,
                        run_id,
                        "STEP_INTERRUPTED",
                        step.step_id,
                        step_key=step.step_key,
                        attempt=step.attempt_count,
                    )

    def begin_step(self, step_id: str, fingerprint: str) -> StepView:
        with Session(self.bind) as session, session.begin():
            step = session.get(Step, step_id)
            assert step is not None
            if step.input_fingerprint and step.input_fingerprint != fingerprint:
                raise RuntimeFault("STEP_INPUT_FINGERPRINT_MISMATCH")
            if step.status == "FAILED" and step.last_failure_class != "RETRYABLE":
                raise RuntimeFault("NONRETRYABLE_STEP_RESTART")
            step.status = transition("step", step.status, "RUNNING")
            step.input_fingerprint = fingerprint
            step.attempt_count += 1
            step.started_at, step.finished_at = utcnow(), None
            self._event(
                session,
                step.run_id,
                "STEP_STARTED",
                step_id,
                step_key=step.step_key,
                attempt=step.attempt_count,
                input_fingerprint=fingerprint,
            )
            return view(StepView, step)

    def commit_step_success(
        self,
        step_id: str,
        expected_input_fingerprint: str,
        artifacts: list[dict[str, Any]],
        after_insert: Callable[[int], None] | None = None,
    ) -> None:
        with Session(self.bind) as session, session.begin():
            step = session.get(Step, step_id)
            assert step is not None
            run = session.execute(
                select(Run).where(Run.run_id == step.run_id).with_for_update()
            ).scalar_one()
            session.refresh(step)
            if step.input_fingerprint != expected_input_fingerprint:
                raise RuntimeFault("STEP_INPUT_FINGERPRINT_MISMATCH")
            expected = {a["artifact_type"]: a for a in artifacts}
            required = (
                {"governed_sql_result"}
                if step.ordinal == 1
                else {"validation_record", "verified_report"}
            )
            allowed = required | ({"evidence_record"} if step.ordinal == 1 else set())
            if (
                not required <= expected.keys()
                or expected.keys() - allowed
                or len(expected) != len(artifacts)
            ):
                raise RuntimeFault("ARTIFACT_SCHEMA_ERROR")
            for a in artifacts:
                decode(
                    a["artifact_type"], a["schema_version"], a["payload"], a["content_fingerprint"]
                )
            if step.ordinal == 1:
                execution = expected["governed_sql_result"]["payload"]
                if (execution["status"] == "EXECUTED") != ("evidence_record" in expected):
                    raise RuntimeFault("ARTIFACT_SCHEMA_ERROR")
            existing = list(session.scalars(select(Artifact).where(Artifact.step_id == step_id)))
            if step.status == "SUCCEEDED":
                for stored in existing:
                    decode(
                        stored.artifact_type,
                        stored.schema_version,
                        stored.payload,
                        stored.content_fingerprint,
                    )
                if {a.artifact_type: a.content_fingerprint for a in existing} != {
                    k: a["content_fingerprint"] for k, a in expected.items()
                }:
                    raise RuntimeFault("ARTIFACT_IDEMPOTENCY_CONFLICT")
                return
            if existing:
                raise RuntimeFault("ARTIFACT_IDEMPOTENCY_CONFLICT")
            if step.status != "RUNNING":
                raise RuntimeFault("INVALID_STATE_TRANSITION")
            if run.status != "RUNNING":
                raise RuntimeFault("INVALID_STATE_TRANSITION")
            keys = []
            for index, a in enumerate(artifacts):
                key = f"run/{run.run_id}/step/{step.step_key}/{a['artifact_type']}"
                keys.append(key)
                session.add(
                    Artifact(
                        task_id=run.task_id,
                        run_id=run.run_id,
                        step_id=step_id,
                        artifact_key=key,
                        **a,
                    )
                )
                session.flush()
                if after_insert:
                    after_insert(index)
            step.status = transition("step", step.status, "SUCCEEDED")
            step.finished_at = utcnow()
            self._event(
                session,
                run.run_id,
                "STEP_SUCCEEDED",
                step_id,
                step_key=step.step_key,
                artifact_keys=keys,
                artifact_fingerprints=[a["content_fingerprint"] for a in artifacts],
            )

    def record_step_failure(self, step_id: str, failure: RuntimeFailure) -> None:
        with Session(self.bind) as session, session.begin():
            step = session.get(Step, step_id)
            assert step is not None
            step.status = transition("step", step.status, "FAILED")
            step.last_failure_class = failure.failure_class
            step.last_error_code = failure.error_code
            step.finished_at = utcnow()
            for kind in ("STEP_ATTEMPT_FAILED", "STEP_FAILED"):
                self._event(
                    session,
                    step.run_id,
                    kind,
                    step_id,
                    step_key=step.step_key,
                    attempt=step.attempt_count,
                    reason_code=failure.error_code,
                    failure_class=failure.failure_class,
                )

    def finalize_run(self, run_id: str, status: str, failure: RuntimeFailure | None = None) -> None:
        with Session(self.bind) as session, session.begin():
            run = session.execute(
                select(Run).where(Run.run_id == run_id).with_for_update()
            ).scalar_one()
            task = session.get(Task, run.task_id)
            assert task is not None
            run.status = transition("run", run.status, status)
            task.status = transition("task", task.status, status)
            run.finished_at = task.updated_at = utcnow()
            if failure:
                run.failure_class, run.failure_code = failure.failure_class, failure.error_code
            for kind in (f"RUN_{status}", f"TASK_{status}"):
                self._event(
                    session,
                    run_id,
                    kind,
                    status=status,
                    reason_code=run.failure_code,
                    failure_class=run.failure_class,
                )

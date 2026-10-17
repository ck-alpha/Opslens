"""Detached domain views; ORM entities never escape persistence."""

from datetime import datetime
from typing import Any

from opslens.sql.models import StrictModel


class TaskView(StrictModel):
    task_id: str
    idempotency_key: str
    request_fingerprint: str
    request_payload: dict[str, Any]
    status: str
    created_at: datetime
    updated_at: datetime


class RunView(StrictModel):
    run_id: str
    task_id: str
    attempt_no: int
    status: str
    runtime_policy_id: str
    runtime_policy_version: str
    runtime_policy_fingerprint: str
    runtime_context_fingerprint: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    failure_class: str | None
    failure_code: str | None


class StepView(StrictModel):
    step_id: str
    run_id: str
    step_key: str
    step_type: str
    ordinal: int
    status: str
    input_fingerprint: str | None
    attempt_count: int
    last_failure_class: str | None
    last_error_code: str | None
    started_at: datetime | None
    finished_at: datetime | None


class ArtifactView(StrictModel):
    artifact_id: str
    task_id: str
    run_id: str
    step_id: str
    artifact_key: str
    artifact_type: str
    schema_version: str
    content_fingerprint: str
    payload: dict[str, Any]
    created_at: datetime


class EventView(StrictModel):
    event_id: str
    run_id: str
    step_id: str | None
    sequence: int
    event_type: str
    payload: dict[str, Any]
    event_fingerprint: str
    created_at: datetime

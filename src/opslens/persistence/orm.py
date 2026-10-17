"""Runtime-only ORM with database constraints and restrictive foreign keys."""

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def identity() -> str:
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','NEEDS_REVIEW','WAITING_INPUT')",
            name="task_status",
        ),
    )
    task_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=identity)
    idempotency_key: Mapped[str] = mapped_column(unique=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(default="PENDING")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        UniqueConstraint("task_id", "attempt_no"),
        CheckConstraint("attempt_no > 0", name="run_attempt"),
        CheckConstraint(
            "status IN ('PENDING','RUNNING','INTERRUPTED',"
            "'SUCCEEDED','FAILED','NEEDS_REVIEW','WAITING_INPUT')",
            name="run_status",
        ),
    )
    run_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=identity)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.task_id", ondelete="RESTRICT"), index=True
    )
    attempt_no: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(default="PENDING")
    runtime_policy_id: Mapped[str]
    runtime_policy_version: Mapped[str]
    runtime_policy_fingerprint: Mapped[str] = mapped_column(String(64))
    runtime_context_fingerprint: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_class: Mapped[str | None]
    failure_code: Mapped[str | None]


class Step(Base):
    __tablename__ = "steps"
    __table_args__ = (
        UniqueConstraint("run_id", "step_key"),
        UniqueConstraint("run_id", "ordinal"),
        CheckConstraint("attempt_count >= 0 AND ordinal > 0", name="step_counts"),
        CheckConstraint(
            "status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','INTERRUPTED','SKIPPED')",
            name="step_status",
        ),
    )
    step_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=identity)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id", ondelete="RESTRICT"), index=True)
    step_key: Mapped[str]
    step_type: Mapped[str]
    ordinal: Mapped[int]
    status: Mapped[str] = mapped_column(default="PENDING")
    input_fingerprint: Mapped[str | None] = mapped_column(String(64))
    attempt_count: Mapped[int] = mapped_column(default=0)
    last_failure_class: Mapped[str | None]
    last_error_code: Mapped[str | None]
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Artifact(Base):
    __tablename__ = "runtime_artifacts"
    artifact_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), primary_key=True, default=identity
    )
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.task_id", ondelete="RESTRICT"))
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id", ondelete="RESTRICT"), index=True)
    step_id: Mapped[str] = mapped_column(
        ForeignKey("steps.step_id", ondelete="RESTRICT"), index=True
    )
    artifact_key: Mapped[str] = mapped_column(unique=True)
    artifact_type: Mapped[str]
    schema_version: Mapped[str]
    content_fingerprint: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Event(Base):
    __tablename__ = "runtime_events"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence"),
        CheckConstraint("sequence > 0", name="event_sequence"),
    )
    event_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=identity)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id", ondelete="RESTRICT"), index=True)
    step_id: Mapped[str | None] = mapped_column(ForeignKey("steps.step_id", ondelete="RESTRICT"))
    sequence: Mapped[int]
    event_type: Mapped[str]
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    event_fingerprint: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

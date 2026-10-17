"""Closed, append-only interaction records, independent of runtime events."""

from datetime import datetime, timedelta
from typing import Literal, get_args
from uuid import UUID

from pydantic import Field, field_validator
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column

from opslens.persistence.orm import Base, identity, utcnow
from opslens.sql.models import StrictModel

EventType = Literal[
    "SESSION_STARTED",
    "METRIC_CLARIFICATION_REQUESTED",
    "METRIC_CLARIFICATION_SHOWN",
    "METRIC_OPTION_SELECTED",
    "DEMO_RECIPE_SELECTED",
    "ANALYSIS_SUBMITTED",
    "RUN_DETAIL_OPENED",
    "EVIDENCE_OPENED",
    "REPORT_EXPORTED",
    "OBSERVABILITY_OPENED",
    "RERUN_REQUESTED",
    "FEEDBACK_HELPFUL",
    "FEEDBACK_NEEDS_CORRECTION",
    "FEEDBACK_UNCLEAR",
]
EVENT_TYPES = get_args(EventType)


class ProductEvent(StrictModel):
    session_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    event_type: EventType
    task_id: UUID | None = None
    run_id: UUID | None = None
    metric_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    metric_version: str | None = Field(default=None, pattern=r"^[0-9]+\.[0-9]+$")
    reason_code: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{0,99}$")


class ProductEventRow(Base):
    __tablename__ = "product_events"
    __table_args__ = (
        CheckConstraint("session_fingerprint ~ '^[a-f0-9]{64}$'", name="product_session_hash"),
        CheckConstraint(
            "event_type IN (" + ",".join("'" + t + "'" for t in EVENT_TYPES) + ")",
            name="product_event_type",
        ),
    )
    event_id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=identity)
    session_fingerprint: Mapped[str] = mapped_column(String(64))
    event_type: Mapped[str] = mapped_column(String(40))
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.task_id", ondelete="RESTRICT"))
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.run_id", ondelete="RESTRICT"))
    metric_id: Mapped[str | None] = mapped_column(String(64))
    metric_version: Mapped[str | None] = mapped_column(String(20))
    reason_code: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class EventWindow(StrictModel):
    since: datetime
    until: datetime

    @field_validator("since", "until")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("UTC_WINDOW_REQUIRED")
        return value


class ProductEventRepository:
    def __init__(self, database: Engine):
        self.database = database

    def append(self, event: ProductEvent) -> None:
        event = ProductEvent.model_validate_json(event.model_dump_json())
        with Session(self.database) as session, session.begin():
            session.add(ProductEventRow(**event.model_dump(mode="json")))

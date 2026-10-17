"""Strict scenario inputs and noise-free observed reliability reports."""

from typing import Any

from pydantic import Field

from opslens.sql.models import StrictModel


class RuntimeCase(StrictModel):
    case_id: str
    scenario: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    expected_task_status: str | None = None
    expected_run_status: str | None = None
    expected_step_statuses: list[str] = Field(default_factory=list)
    expected_attempt_counts: list[int] = Field(default_factory=list)
    expected_event_types: list[str] = Field(default_factory=list)
    expected_reason_codes: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)

"""Explicit independent expectations for validation tests only."""

from typing import Literal

from pydantic import Field

from opslens.sql.models import StrictModel
from opslens.validation.intent import IntentFilter, TimeWindow
from opslens.validation.models import ValidationMode, ValidationStatus


class ValidationCase(StrictModel):
    case_id: str
    category: str
    sql: str
    metric_id: str | None
    metric_version: str
    output: str
    expected_status: ValidationStatus
    expected_reason: str
    action: str | None = None
    mode: ValidationMode = "METRIC_ANALYSIS"
    time_window: TimeWindow | None = None
    dimensions: list[str] = Field(default_factory=list)
    filters: list[IntentFilter] = Field(default_factory=list)
    time_grain: Literal["day", "week", "month"] | None = None
    allow_deprecated: bool = False
    states: list[ValidationStatus] = Field(default_factory=list)


class ReportCase(StrictModel):
    case_id: str
    source: str
    action: str | None = None
    expected_status: ValidationStatus
    expected_reason: str

"""Facts are structured result slices, never caller-authored prose."""

from datetime import datetime, timedelta
from typing import Literal

from pydantic import JsonValue, field_validator

from opslens.sql.models import StrictModel
from opslens.validation.models import ValidationStatus, ValidatorResult


class VerifiedFact(StrictModel):
    kind: Literal["VERIFIED_BUSINESS_METRIC", "VERIFIED_DATA_RESULT"]
    metric_id: str | None
    metric_version: str | None
    metric_output_column: str | None
    unit: str | None
    columns: list[str]
    rows: list[list[JsonValue]]
    total_row_count: int
    truncated: bool


class InterpretationItem(StrictModel):
    kind: Literal["DESCRIPTIVE", "CAUSAL"]
    text: str
    status: ValidationStatus
    reason_code: str


class UncertaintyItem(StrictModel):
    validator: str
    reason_code: str
    summary: str


class VerifiedReport(StrictModel):
    report_version: Literal["m6.0"] = "m6.0"
    overall_status: ValidationStatus
    validation_fingerprint: str
    evidence_content_fingerprint: str | None
    title: str = "OpsLens Verified Report"
    verified_facts: list[VerifiedFact]
    interpretations: list[InterpretationItem]
    uncertainties: list[UncertaintyItem]
    validation_summary: list[ValidatorResult]
    warnings: list[str]
    generated_at: datetime
    report_fingerprint: str

    @field_validator("generated_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("UTC_AWARE_TIMESTAMP_REQUIRED")
        return value

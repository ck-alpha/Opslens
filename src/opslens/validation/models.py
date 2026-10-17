"""Derivative validation records; never modify source evidence."""

from datetime import datetime, timedelta
from typing import Literal

from pydantic import Field, JsonValue, field_validator

from opslens.sql.models import StrictModel

ValidationStatus = Literal["PASS", "FAIL", "UNKNOWN", "NEEDS_REVIEW"]
ValidationMode = Literal["METRIC_ANALYSIS", "DATA_QUERY"]
ValidatorName = Literal["EXECUTION", "EVIDENCE", "METRIC", "RESPONSE"]


class ValidationCheck(StrictModel):
    check_id: str
    status: ValidationStatus
    reason_code: str
    summary: str
    required: bool = True
    applicable: bool = True
    details: dict[str, JsonValue] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)


class ValidatorResult(StrictModel):
    validator: ValidatorName
    status: ValidationStatus
    checks: list[ValidationCheck]
    reason_codes: list[str]


class ValidationRecord(StrictModel):
    validation_version: Literal["m6.0"] = "m6.0"
    evidence_content_fingerprint: str | None
    validation_policy_id: str
    validation_policy_version: str
    validation_policy_fingerprint: str
    request_fingerprint: str
    execution: ValidatorResult
    evidence: ValidatorResult
    metric: ValidatorResult
    response: ValidatorResult
    overall_status: ValidationStatus
    reason_codes: list[str]
    validation_fingerprint: str
    evaluated_at: datetime

    @field_validator("evaluated_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("UTC_AWARE_TIMESTAMP_REQUIRED")
        return value

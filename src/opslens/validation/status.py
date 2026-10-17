"""Deterministic precedence and exception containment without exception messages."""

from collections.abc import Callable, Iterable

from opslens.evidence.hashing import digest
from opslens.validation.models import (
    ValidationCheck,
    ValidationRecord,
    ValidationStatus,
    ValidatorName,
    ValidatorResult,
)

PRIORITY: tuple[ValidationStatus, ...] = ("PASS", "UNKNOWN", "NEEDS_REVIEW", "FAIL")


def aggregate(checks: Iterable[ValidationCheck]) -> ValidationStatus:
    statuses = [c.status for c in checks if c.required and c.applicable]
    return PRIORITY[max((PRIORITY.index(s) for s in statuses), default=0)]


def check(code: str, status: ValidationStatus = "PASS", **kwargs) -> ValidationCheck:
    return ValidationCheck(
        check_id=code,
        reason_code=code,
        status=status,
        summary=code.replace("_", " ").capitalize(),
        **kwargs,
    )


def result(name: ValidatorName, checks: list[ValidationCheck]) -> ValidatorResult:
    return ValidatorResult(
        validator=name,
        status=aggregate(checks),
        checks=checks,
        reason_codes=sorted({c.reason_code for c in checks}),
    )


def guarded(name: ValidatorName, run: Callable[[], list[ValidationCheck]]) -> ValidatorResult:
    try:
        checks = [ValidationCheck.model_validate(c.model_dump()) for c in run()]
        if not checks:
            raise ValueError("EMPTY_VALIDATOR_RESULT")
        return result(name, checks)
    except Exception as exc:
        return result(
            name,
            [
                check(
                    "VALIDATOR_EXCEPTION",
                    "NEEDS_REVIEW",
                    details={"error_type": type(exc).__name__},
                )
            ],
        )


def validation_fingerprint(record: ValidationRecord) -> str:
    return digest(
        record.model_dump(mode="json", exclude={"evaluated_at", "validation_fingerprint"})
    )

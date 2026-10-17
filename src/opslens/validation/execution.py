"""Map governed SQL execution outcomes without weakening governance."""

from opslens.sql.models import GovernedSQLResult
from opslens.validation.models import ValidationCheck, ValidationStatus
from opslens.validation.status import check


def validate_execution(execution: GovernedSQLResult) -> list[ValidationCheck]:
    execution = GovernedSQLResult.model_validate(execution.model_dump())
    if execution.status == "EXECUTED" and execution.decision.decision != "ALLOW":
        return [check("EXECUTION_POLICY_INCONSISTENT", "FAIL")]
    states: dict[str, tuple[ValidationStatus, str]] = {
        "EXECUTED": ("PASS", "EXECUTION_SUCCEEDED"),
        "REJECTED": ("FAIL", "EXECUTION_REJECTED"),
        "TIMEOUT": ("NEEDS_REVIEW", "EXECUTION_TIMEOUT"),
        "ERROR": ("NEEDS_REVIEW", "EXECUTION_ERROR"),
    }
    status, code = states[execution.status]
    return [
        check(code, status, details={"primary_reason_code": execution.decision.primary_reason_code})
    ]

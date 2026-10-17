"""Only typed, explicitly safe failures may be retried. Never persist messages."""

from typing import Literal

from opslens.sql.models import StrictModel

FailureClass = Literal["RETRYABLE", "NON_RETRYABLE", "NEEDS_USER_INPUT", "NEEDS_REVIEW"]


class RuntimeFailure(StrictModel):
    failure_class: FailureClass
    error_code: str
    error_type: str | None = None


class RuntimeFault(Exception):
    failure_class: FailureClass = "NEEDS_REVIEW"

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class RetryableRuntimeError(RuntimeFault):
    failure_class: FailureClass = "RETRYABLE"


class NonRetryableRuntimeError(RuntimeFault):
    failure_class: FailureClass = "NON_RETRYABLE"


class NeedsUserInputRuntimeError(RuntimeFault):
    failure_class: FailureClass = "NEEDS_USER_INPUT"


def classify(exc: Exception) -> RuntimeFailure:
    return RuntimeFailure(
        failure_class=exc.failure_class if isinstance(exc, RuntimeFault) else "NEEDS_REVIEW",
        error_code=exc.code if isinstance(exc, RuntimeFault) else "UNEXPECTED_STEP_EXCEPTION",
        error_type=type(exc).__name__,
    )

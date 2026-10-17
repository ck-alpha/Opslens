"""Closed model allowlist plus constrained scalar values; never serialize payloads."""

import re
from datetime import datetime

from pydantic import BaseModel

IDENTIFIER = re.compile(r"[A-Za-z0-9_.:@+\-]+\Z")
CODE = re.compile(r"[A-Z][A-Z0-9_]*\Z")
CODES = {
    "status",
    "final_status",
    "failure_class",
    "failure_code",
    "last_failure_class",
    "last_error_code",
    "validation_status",
    "policy_decision",
    "reason_codes",
    "terminal_event",
    "accounting_mode",
    "unavailable_metrics",
}


def assert_safe(model: BaseModel) -> None:
    def walk(value, field=""):
        if isinstance(value, BaseModel):
            for name in type(value).model_fields:
                walk(getattr(value, name), name)
        elif isinstance(value, dict):
            for key, item in value.items():
                walk(key)
                walk(item, field)
        elif isinstance(value, list):
            for item in value:
                walk(item, field)
        elif isinstance(value, str):
            pattern = CODE if field in CODES else IDENTIFIER
            if not pattern.fullmatch(value) or value.lower().startswith(("sk-", "bearer")):
                raise ValueError("OBSERVABILITY_UNSAFE_IDENTIFIER")
        elif value is not None and not isinstance(value, (bool, int, float, datetime)):
            raise ValueError("OBSERVABILITY_UNSAFE_VALUE")

    walk(model)

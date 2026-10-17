"""Strict versioned synchronous runtime policy."""

from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from opslens.evidence.hashing import digest
from opslens.sql.models import StrictModel


class RuntimePolicy(StrictModel):
    runtime_id: str = "opslens_persistent_runtime"
    version: str = "1.0"
    max_step_attempts: int = Field(default=3, ge=1, le=20)
    retry_backoff_ms: list[int] = Field(default_factory=lambda: [0, 100, 500])
    advisory_lock_namespace: Literal[7001] = 7001
    persist_result_rows: Literal[True] = True

    @model_validator(mode="after")
    def valid_backoff(self) -> "RuntimePolicy":
        if len(self.retry_backoff_ms) != self.max_step_attempts or any(
            n < 0 or n > 60000 for n in self.retry_backoff_ms
        ):
            raise ValueError("INVALID_RETRY_BACKOFF")
        return self

    def fingerprint(self) -> str:
        return digest(self.model_dump(mode="json"))

    @classmethod
    def load(cls, path: Path) -> "RuntimePolicy":
        return cls.model_validate_json(path.read_text("utf-8"))

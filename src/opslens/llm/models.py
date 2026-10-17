"""Bounded provider contracts. Raw provider responses and thinking are never retained."""

from typing import Literal, Protocol

from pydantic import Field

from opslens.sql.models import StrictModel


class Candidate(StrictModel):
    sql: str = Field(min_length=1, max_length=20000)
    assumptions: list[str] = Field(default_factory=list, max_length=8)
    schema_entities_used: list[str] = Field(default_factory=list, max_length=100)


class GenerationMetadata(StrictModel):
    provider: Literal["ollama", "fake"]
    model: str = Field(pattern=r"^[A-Za-z0-9_.:@+\-]{1,120}$")
    prompt_version: str = "m11.0"
    prompt_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    attempt: int = Field(ge=1, le=2)
    duration_ms: float = Field(ge=0, allow_inf_nan=False)
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    finish_reason: Literal["stop", "length", "unknown"] = "unknown"
    status: Literal[
        "SUCCESS",
        "MALFORMED_OUTPUT",
        "PROVIDER_TIMEOUT",
        "PROVIDER_UNAVAILABLE",
        "PROVIDER_ERROR",
        "CONTEXT_BUDGET_EXCEEDED",
    ]
    candidate_sql_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class Generation(StrictModel):
    candidate: Candidate | None = None
    metadata: GenerationMetadata


class SQLGenerator(Protocol):
    def generate(self, prompt: str, attempt: int) -> Generation: ...

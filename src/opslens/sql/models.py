"""Serializable governance metadata; AST objects never cross the service boundary."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class SQLAnalysis(StrictModel):
    original_sql: str
    normalized_sql: str | None = None
    statement_count: int = 0
    statement_type: str | None = None
    schemas: list[str] = Field(default_factory=list)
    physical_tables: list[str] = Field(default_factory=list)
    columns: list[str] = Field(default_factory=list)
    functions: list[str] = Field(default_factory=list)
    table_functions: list[str] = Field(default_factory=list)
    join_count: int = 0
    cte_count: int = 0
    subquery_count: int = 0
    table_reference_count: int = 0
    top_level_limit: int | None = None
    has_projection_star: bool = False
    parse_error: str | None = None


class PolicyDecision(StrictModel):
    decision: Literal["ALLOW", "REJECT"]
    primary_reason_code: str
    reason_codes: list[str]
    policy_id: str
    policy_version: str
    policy_fingerprint: str
    analysis: SQLAnalysis
    effective_sql: str | None = None
    query_fingerprint: str | None = None
    warnings: list[str] = Field(default_factory=list)


class GovernedSQLResult(StrictModel):
    status: Literal["EXECUTED", "REJECTED", "TIMEOUT", "ERROR"]
    decision: PolicyDecision
    columns: list[str] = Field(default_factory=list)
    rows: list[list[JsonValue]] = Field(default_factory=list)
    row_count: int = 0
    error_code: str | None = None
    error_type: str | None = None
    elapsed_ms: float | None = None

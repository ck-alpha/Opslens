"""Versioned, fail-fast configuration and semantic policy fingerprints."""

import hashlib
import json
import re
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator

from opslens.sql.models import StrictModel

RULE_SETS = (
    "allowed_schemas",
    "allowed_tables",
    "denied_columns",
    "forbidden_functions",
    "forbidden_table_functions",
)


class SQLPolicyConfig(StrictModel):
    policy_id: str = Field(min_length=1)
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+(?:\.[0-9]+)?$")
    dialect: Literal["duckdb"]
    # Tuples prevent mutation of rules after validation/fingerprinting.
    allowed_schemas: tuple[str, ...]
    allowed_tables: tuple[str, ...]
    denied_columns: tuple[str, ...]
    forbidden_functions: tuple[str, ...]
    forbidden_table_functions: tuple[str, ...]
    allow_star_projection: bool
    max_result_rows: int = Field(gt=0)
    max_joins: int = Field(gt=0)
    max_ctes: int = Field(gt=0)
    max_subqueries: int = Field(gt=0)
    max_table_references: int = Field(gt=0)
    max_sql_length: int = Field(gt=0)
    timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    external_access: Literal[False]

    @model_validator(mode="after")
    def validate_rules(self) -> Self:
        for field in RULE_SETS:
            values = getattr(self, field)
            if len(values) != len(set(values)):
                raise ValueError(f"Duplicate rules: {field}")
            if any(not re.fullmatch(r"[a-z_][a-z_0-9]*(?:\.[a-z_][a-z_0-9]*)?", v) for v in values):
                raise ValueError(f"Rules must be canonical lowercase identifiers: {field}")
        if self.allowed_schemas != ("main",) or not self.allowed_tables:
            raise ValueError(
                "Governed SQL requires main schema and a nonempty physical table allowlist"
            )
        if any("." in t for t in self.allowed_tables):
            raise ValueError("Use unqualified table names")
        return self

    @classmethod
    def load(cls, path: Path) -> Self:
        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    def fingerprint(self) -> str:
        data = self.model_dump(mode="json")
        for field in RULE_SETS:
            data[field] = sorted(data[field])
        canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(canonical.encode()).hexdigest()

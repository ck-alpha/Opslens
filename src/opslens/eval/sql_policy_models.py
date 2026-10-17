"""Independent SQL governance benchmark inputs, unavailable to runtime."""

from pathlib import Path
from typing import Literal

from pydantic import Field

from opslens.sql.models import StrictModel


class SQLPolicyCase(StrictModel):
    id: str = Field(min_length=1)
    sql: str
    expected_decision: Literal["ALLOW", "REJECT"]
    expected_primary_reason_code: str
    execute_if_allowed: bool = True
    expected_tables: list[str] | None = None
    expected_denied_columns: list[str] = Field(default_factory=list)
    tags: list[str]


def load_sql_policy_cases(path: Path) -> list[SQLPolicyCase]:
    cases = [
        SQLPolicyCase.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not cases or len({c.id for c in cases}) != len(cases):
        raise ValueError("Empty benchmark or duplicate case IDs")
    if not any(c.expected_decision == "ALLOW" for c in cases) or not any(
        c.expected_decision == "REJECT" for c in cases
    ):
        raise ValueError("Benchmark requires safe and unsafe cases")
    return cases

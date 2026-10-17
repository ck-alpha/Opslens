"""Independent provenance expectations; runtime never reads these cases."""

from typing import Literal

from pydantic import Field

from opslens.evidence.models import ClaimBinding, MetricBinding
from opslens.sql.models import StrictModel


class ExpectedOutput(StrictModel):
    output_name: str
    source_columns: list[str]
    source_tables: list[str]
    dependency_kind: Literal["COLUMN_VALUE", "ROWSET", "CONSTANT", "MIXED"]
    lineage_status: Literal["RESOLVED", "UNRESOLVED"]


class ExpectedContext(StrictModel):
    kind: Literal["WHERE", "HAVING", "JOIN_ON", "GROUP_BY"]
    source_columns: list[str]
    status: Literal["RESOLVED", "UNRESOLVED"]


class LineageCase(StrictModel):
    case_id: str
    sql: str
    expected_outputs: list[ExpectedOutput] = Field(min_length=1)
    expected_context: list[ExpectedContext]
    expected_status: Literal["RESOLVED", "PARTIAL", "UNRESOLVED"]
    expected_source_tables: list[str]


class EvidenceCase(StrictModel):
    case_id: str
    sql: str
    metric_ref: MetricBinding | None
    citation_query: str | None
    citation_count: int = Field(ge=0, le=5)
    claim: ClaimBinding | None
    expected_completeness: Literal["COMPLETE", "PARTIAL", "UNRESOLVED"]

"""Strict serializable column and query-context provenance."""

from typing import Literal

from pydantic import Field

from opslens.sql.models import StrictModel


class PhysicalColumnRef(StrictModel):
    schema_name: str = Field(alias="schema", serialization_alias="schema")
    table: str
    column: str

    @property
    def canonical(self) -> str:
        return f"{self.schema_name}.{self.table}.{self.column}"


class OutputColumnProvenance(StrictModel):
    output_name: str
    output_ordinal: int
    expression_sql: str
    source_columns: list[PhysicalColumnRef]
    source_tables: list[str]
    dependency_kind: Literal["COLUMN_VALUE", "ROWSET", "CONSTANT", "MIXED"]
    lineage_status: Literal["RESOLVED", "UNRESOLVED"]
    reason_code: str


class QueryContextExpression(StrictModel):
    kind: Literal["WHERE", "HAVING", "JOIN_ON", "GROUP_BY"]
    expression_sql: str
    source_columns: list[PhysicalColumnRef]
    status: Literal["RESOLVED", "UNRESOLVED"]
    reason_code: str


class QueryProvenance(StrictModel):
    provenance_version: Literal["m5.0"] = "m5.0"
    query_fingerprint: str
    effective_sql: str
    source_tables: list[str]
    output_columns: list[OutputColumnProvenance]
    context: list[QueryContextExpression]
    lineage_status: Literal["RESOLVED", "PARTIAL", "UNRESOLVED"]
    reason_codes: list[str]
    provenance_fingerprint: str


class DataSnapshot(StrictModel):
    snapshot_version: Literal["m5.0"] = "m5.0"
    snapshot_kind: Literal["POST_EXECUTION_LOGICAL_SNAPSHOT"] = "POST_EXECUTION_LOGICAL_SNAPSHOT"
    generator_version: str
    schema_version: str
    seed: int
    timezone: str
    synthetic: Literal[True]
    referenced_tables: list[str]
    table_row_counts: dict[str, int]
    table_fingerprints: dict[str, str]
    snapshot_fingerprint: str

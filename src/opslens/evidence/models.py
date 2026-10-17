"""Evidence records bind declared business context to an execution, without validation."""

from datetime import datetime, timedelta
from typing import Literal

from pydantic import Field, field_validator

from opslens.knowledge.models import KnowledgeCitation
from opslens.provenance.models import DataSnapshot, QueryProvenance
from opslens.sql.models import GovernedSQLResult, StrictModel


class MetricBinding(StrictModel):
    metric_id: str
    version: str
    output_columns: list[str] = Field(min_length=1)


class MetricEvidenceRef(MetricBinding):
    status: Literal["ACTIVE", "DEPRECATED"]
    metric_registry_fingerprint: str
    binding_status: Literal["DECLARED_NOT_VALIDATED"] = "DECLARED_NOT_VALIDATED"


class ClaimBinding(StrictModel):
    claim_id: str = Field(min_length=1)
    claim_text: str = Field(min_length=1)
    output_columns: list[str] = Field(min_length=1)
    binding_status: Literal["LINKED_NOT_VALIDATED"] = "LINKED_NOT_VALIDATED"


class KnowledgeEvidenceRef(StrictModel):
    citation: KnowledgeCitation
    corpus_fingerprint: str
    document_content_sha256: str
    chunk_verification: Literal["UPSTREAM_FINGERPRINT_PRESERVED"] = "UPSTREAM_FINGERPRINT_PRESERVED"


class ResultEvidence(StrictModel):
    columns: list[str]
    row_count: int = Field(ge=0)
    result_hash: str


class EvidenceRecord(StrictModel):
    evidence_version: Literal["m5.0"] = "m5.0"
    task_id: str | None = None
    run_id: str | None = None
    claim: ClaimBinding | None = None
    metric: MetricEvidenceRef | None = None
    policy_id: str
    policy_version: str
    policy_fingerprint: str
    original_sql: str
    normalized_sql: str | None
    effective_sql: str
    query_fingerprint: str
    provenance: QueryProvenance
    data_snapshot: DataSnapshot
    result: ResultEvidence
    knowledge: list[KnowledgeEvidenceRef]
    validation_status: Literal["NOT_EVALUATED"] = "NOT_EVALUATED"
    completeness_status: Literal["COMPLETE", "PARTIAL", "UNRESOLVED"]
    reason_codes: list[str]
    content_fingerprint: str
    recorded_at: datetime

    @field_validator("recorded_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("UTC_AWARE_TIMESTAMP_REQUIRED")
        return value


class EvidenceExecutionResult(StrictModel):
    execution: GovernedSQLResult
    evidence: EvidenceRecord | None = None


class LedgerEntry(StrictModel):
    sequence: int = Field(ge=1)
    previous_entry_hash: str | None
    evidence: EvidenceRecord
    entry_hash: str

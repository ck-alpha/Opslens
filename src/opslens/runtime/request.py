"""Portable request identity; infrastructure dependencies are supplied separately."""

from pydantic import Field

from opslens.evidence.hashing import digest
from opslens.evidence.models import ClaimBinding, MetricBinding
from opslens.knowledge.models import KnowledgeCitation
from opslens.llm.models import GenerationMetadata
from opslens.reporting.models import InterpretationItem
from opslens.sql.models import StrictModel
from opslens.validation.intent import AnalysisIntent
from opslens.validation.models import ValidationMode


class RuntimeAnalysisRequest(StrictModel):
    candidate_sql: str = Field(min_length=1)
    validation_mode: ValidationMode = "DATA_QUERY"
    metric_ref: MetricBinding | None = None
    intent: AnalysisIntent | None = None
    claim: ClaimBinding | None = None
    citations: list[KnowledgeCitation] = Field(default_factory=list)
    interpretations: list[InterpretationItem] = Field(default_factory=list)

    generation: list[GenerationMetadata] | None = Field(
        default=None, max_length=2, exclude_if=lambda value: value is None
    )

    def fingerprint(self) -> str:
        return digest(self.model_dump(mode="json"))

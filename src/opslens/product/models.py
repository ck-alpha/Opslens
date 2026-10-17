"""Product contracts: no runtime payloads cross the presentation boundary."""

from datetime import datetime
from typing import Literal

from pydantic import Field

from opslens.llm.models import GenerationMetadata
from opslens.observability.models import RunObservation
from opslens.reporting.models import VerifiedReport
from opslens.runtime.request import RuntimeAnalysisRequest
from opslens.sql.models import StrictModel

ProductStatus = Literal[
    "CLARIFICATION_REQUIRED",
    "READY_TO_RUN",
    "RUNNING",
    "VERIFIED",
    "NEEDS_REVIEW",
    "REJECTED",
    "FAILED",
]


class DemoAnalysisRecipe(StrictModel):
    recipe_id: str = Field(pattern=r"^[a-z][a-z0-9_]+$")
    title: str
    description: str
    question: str
    runtime_request: RuntimeAnalysisRequest
    category: Literal["VERIFIED_ANALYSIS", "POLICY_DEMO", "REVIEW_DEMO"]
    tags: list[str]


class EvidenceSummary(StrictModel):
    completeness: str
    provenance_status: str
    query_fingerprint: str
    policy_fingerprint: str
    data_snapshot_fingerprint: str
    evidence_fingerprint: str
    citation_identities: list[str]


class TimelineEvent(StrictModel):
    sequence: int
    event_type: str
    step_id: str | None
    created_at: datetime


class ProductRunView(StrictModel):
    task_id: str
    run_id: str
    status: ProductStatus
    metric_id: str | None
    metric_version: str | None
    created_at: datetime
    finished_at: datetime | None
    headline: str
    reason_codes: list[str]
    has_verified_facts: bool
    has_uncertainty: bool
    can_resume: bool
    can_rerun: bool
    report: VerifiedReport | None
    observation: RunObservation
    evidence: EvidenceSummary | None
    timeline: list[TimelineEvent]
    generation: list[GenerationMetadata] = Field(default_factory=list)

"""Structured clarification actions and diagnostic evidence availability."""

from typing import Literal

from pydantic import Field

from opslens.knowledge.models import KnowledgeCitation
from opslens.metrics.models import ContractModel, MetricResolution

ClarificationAction = Literal["PROCEED", "ASK_USER", "NEEDS_REVIEW"]
ClarificationReason = Literal[
    "METRIC_RESOLVED",
    "AMBIGUOUS_METRIC",
    "UNKNOWN_ANALYTIC_METRIC",
    "DEPRECATED_METRIC_VERSION",
    "METRIC_REGISTRY_CONFLICT",
]


class ClarificationOption(ContractModel):
    metric_id: str
    version: str
    status: Literal["ACTIVE", "DEPRECATED"]
    label: str
    description: str
    unit: str
    time_column: str
    refund_policy: str
    citation: KnowledgeCitation | None = None


class ClarificationDecision(ContractModel):
    action: ClarificationAction
    query: str
    reason_code: ClarificationReason
    metric_resolution: MetricResolution
    question: str | None = None
    options: list[ClarificationOption] = Field(default_factory=list)
    citations: list[KnowledgeCitation] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)

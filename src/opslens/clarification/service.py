"""Registry-authoritative policy for requests already classified as analytic."""

from opslens.clarification.models import (
    ClarificationAction,
    ClarificationDecision,
    ClarificationOption,
    ClarificationReason,
)
from opslens.knowledge.index import BusinessKnowledgeIndex
from opslens.metrics import MetricRegistry, MetricResolver
from opslens.metrics.models import ResolutionStatus

POLICY: dict[ResolutionStatus, tuple[ClarificationAction, ClarificationReason, str | None]] = {
    "RESOLVED": ("PROCEED", "METRIC_RESOLVED", None),
    "AMBIGUOUS": ("ASK_USER", "AMBIGUOUS_METRIC", "存在多个业务口径，请确认要分析哪一个指标。"),
    "UNKNOWN": (
        "ASK_USER",
        "UNKNOWN_ANALYTIC_METRIC",
        "请明确希望使用哪个指标评价业务表现，并确认指标版本。",
    ),
    "DEPRECATED": (
        "ASK_USER",
        "DEPRECATED_METRIC_VERSION",
        "请求的指标版本已废弃，请确认使用历史版本还是现行版本。",
    ),
    "CONFLICT": ("NEEDS_REVIEW", "METRIC_REGISTRY_CONFLICT", None),
}


class MetricClarificationService:
    def __init__(self, registry: MetricRegistry, index: BusinessKnowledgeIndex | None = None):
        self.registry = registry
        self.resolver = MetricResolver(registry)
        self.index = index

    async def clarify(
        self, query: str, requested_version: str | None = None
    ) -> ClarificationDecision:
        resolution = self.resolver.resolve(query, version=requested_version)
        action, reason, question = POLICY[resolution.status]
        definitions = []
        for candidate in resolution.candidates:
            definition = self.registry.get(candidate.metric_id, candidate.version)
            if definition is not None:
                definitions.append(definition)
        if resolution.status == "DEPRECATED":
            for candidate in resolution.candidates:
                active = self.registry.active(candidate.metric_id)
                if active is not None:
                    definitions.append(active)
        options = []
        citations = []
        diagnostics = []
        for definition in definitions:
            citation = None
            if self.index is None:
                diagnostics.append("KNOWLEDGE_UNAVAILABLE:NOT_CONFIGURED")
            elif self.index.corpus.registry_fingerprint != self.registry.fingerprint():
                diagnostics.append("KNOWLEDGE_UNAVAILABLE:REGISTRY_SNAPSHOT_MISMATCH")
            else:
                evidence = await self.index.search(
                    definition.metric_id,
                    top_k=1,
                    knowledge_types=["METRIC_CONTRACT"],
                    doc_id=f"metric:{definition.metric_id}",
                    version=definition.version,
                    include_deprecated=definition.status == "DEPRECATED",
                    min_similarity=-1.0,
                )
                if evidence.hits:
                    citation = evidence.hits[0].citation
                    citations.append(citation)
                else:
                    diagnostics.append(
                        f"KNOWLEDGE_UNAVAILABLE:{evidence.error_code or evidence.status}"
                    )
            # RESOLVED has supporting evidence but no unnecessary selection menu.
            if resolution.status != "RESOLVED":
                options.append(
                    ClarificationOption(
                        metric_id=definition.metric_id,
                        version=definition.version,
                        status=definition.status,
                        label=definition.name,
                        description=definition.description,
                        unit=definition.unit,
                        time_column=definition.time_semantics.time_column,
                        refund_policy=definition.refund_policy,
                        citation=citation,
                    )
                )
        return ClarificationDecision(
            action=action,
            query=query,
            reason_code=reason,
            metric_resolution=resolution,
            question=question,
            options=options,
            citations=citations,
            diagnostics=sorted(set(diagnostics)),
        )

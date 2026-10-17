"""Candidate SQL can only reach the database through GovernedSQLService."""

from collections.abc import Sequence

from opslens.evidence.builder import EvidenceBuilder
from opslens.evidence.models import ClaimBinding, EvidenceExecutionResult, MetricBinding
from opslens.knowledge.models import KnowledgeCitation
from opslens.sql.service import GovernedSQLService


class EvidenceExecutionService:
    def __init__(self, governed: GovernedSQLService, builder: EvidenceBuilder):
        self.governed = governed
        self.builder = builder

    def execute_with_evidence(
        self,
        sql: str,
        *,
        metric_ref: MetricBinding | None = None,
        claim: ClaimBinding | None = None,
        citations: Sequence[KnowledgeCitation] = (),
        task_id: str | None = None,
        run_id: str | None = None,
    ) -> EvidenceExecutionResult:
        self.builder.validate_bindings(metric_ref, claim, citations)
        result = self.governed.execute(sql)
        evidence = None
        if result.status == "EXECUTED":
            evidence = self.builder.build(
                result,
                metric_ref=metric_ref,
                claim=claim,
                citations=citations,
                task_id=task_id,
                run_id=run_id,
            )
        return EvidenceExecutionResult(execution=result, evidence=evidence)

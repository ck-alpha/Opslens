"""Compose executed SQL, provenance, logical snapshot, and explicit business bindings."""

from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path

from opslens.evidence.hashing import digest, result_hash
from opslens.evidence.models import (
    ClaimBinding,
    EvidenceRecord,
    KnowledgeEvidenceRef,
    MetricBinding,
    MetricEvidenceRef,
    ResultEvidence,
)
from opslens.knowledge.corpus import KnowledgeCorpus
from opslens.knowledge.models import KnowledgeCitation
from opslens.metrics import MetricRegistry
from opslens.provenance import ProvenanceBuilder
from opslens.provenance.snapshot import capture_snapshot
from opslens.sql.models import GovernedSQLResult
from opslens.sql.schema import DatabaseSchemaSnapshot


def content_fingerprint(record: EvidenceRecord) -> str:
    return digest(
        record.model_dump(
            mode="json", by_alias=True, exclude={"content_fingerprint", "recorded_at"}
        )
    )


class EvidenceBuilder:
    def __init__(
        self,
        db: Path,
        schema: DatabaseSchemaSnapshot,
        registry: MetricRegistry,
        corpus: KnowledgeCorpus,
        clock: Callable[[], datetime] | None = None,
    ):
        if registry.fingerprint() != corpus.registry_fingerprint:
            raise ValueError("REGISTRY_CORPUS_MISMATCH")
        self.db = db
        self.provenance = ProvenanceBuilder(schema)
        self.registry = registry
        self.corpus = corpus
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def validate_bindings(
        self,
        metric_ref: MetricBinding | None,
        claim: ClaimBinding | None,
        citations: Sequence[KnowledgeCitation],
    ) -> None:
        if metric_ref:
            MetricBinding.model_validate(metric_ref.model_dump())
            if self.registry.get(metric_ref.metric_id, metric_ref.version) is None:
                raise ValueError("UNKNOWN_METRIC_VERSION")
        if claim:
            ClaimBinding.model_validate(claim.model_dump())
        for citation in citations:
            self.knowledge_ref(citation)

    def knowledge_ref(self, citation: KnowledgeCitation) -> KnowledgeEvidenceRef:
        citation = KnowledgeCitation.model_validate(citation.model_dump())
        document = next(
            (
                d
                for d in self.corpus.documents
                if (d.doc_id, d.version, d.source_ref, d.title, d.knowledge_type)
                == (
                    citation.doc_id,
                    citation.version,
                    citation.source_ref,
                    citation.title,
                    citation.knowledge_type,
                )
            ),
            None,
        )
        if document is None:
            raise ValueError("INVALID_CITATION_IDENTITY")
        return KnowledgeEvidenceRef(
            citation=citation,
            corpus_fingerprint=self.corpus.fingerprint,
            document_content_sha256=document.content_sha256,
        )

    def build(
        self,
        result: GovernedSQLResult,
        *,
        metric_ref: MetricBinding | None = None,
        claim: ClaimBinding | None = None,
        citations: Sequence[KnowledgeCitation] = (),
        task_id: str | None = None,
        run_id: str | None = None,
    ) -> EvidenceRecord:
        self.validate_bindings(metric_ref, claim, citations)
        provenance = self.provenance.build(result)
        metric = None
        for binding in (metric_ref, claim):
            if binding and (
                not set(binding.output_columns) <= set(result.columns)
                or any(result.columns.count(n) != 1 for n in binding.output_columns)
            ):
                raise ValueError("INVALID_BINDING_OUTPUT")
        if metric_ref:
            definition = self.registry.get(metric_ref.metric_id, metric_ref.version)
            assert definition is not None
            metric = MetricEvidenceRef(
                **metric_ref.model_dump(),
                status=definition.status,
                metric_registry_fingerprint=self.registry.fingerprint(),
            )
        snapshot = capture_snapshot(
            self.db, [f"main.{t}" for t in result.decision.analysis.physical_tables]
        )
        decision = result.decision
        record = EvidenceRecord(
            task_id=task_id,
            run_id=run_id,
            claim=claim,
            metric=metric,
            policy_id=decision.policy_id,
            policy_version=decision.policy_version,
            policy_fingerprint=decision.policy_fingerprint,
            original_sql=decision.analysis.original_sql,
            normalized_sql=decision.analysis.normalized_sql,
            effective_sql=provenance.effective_sql,
            query_fingerprint=provenance.query_fingerprint,
            provenance=provenance,
            data_snapshot=snapshot,
            result=ResultEvidence(
                columns=result.columns, row_count=result.row_count, result_hash=result_hash(result)
            ),
            knowledge=[self.knowledge_ref(c) for c in citations],
            completeness_status="COMPLETE"
            if provenance.lineage_status == "RESOLVED"
            else provenance.lineage_status,
            reason_codes=provenance.reason_codes,
            content_fingerprint="",
            recorded_at=self.clock(),
        )
        return record.model_copy(
            update={"content_fingerprint": content_fingerprint(record)}, deep=True
        )

"""Composition over pinned Lumen chunking, embeddings, search and filters."""

import math

from lumen.ai.embeddings import NumpyEmbeddings
from lumen.ai.vector_store import NumpyVectorStore, VectorStore

from opslens.knowledge.citations import citation_for
from opslens.knowledge.corpus import KnowledgeCorpus
from opslens.knowledge.models import (
    KnowledgeHit,
    KnowledgeMetadata,
    KnowledgeSearchResult,
    KnowledgeType,
)

EXCLUDED_METADATA = [
    "llm_context",
    "content_sha256",
    "metric_registry_fingerprint",
    "source_ref",
    "filename",
    "doc_id",
    "doc_version",
    "source_kind",
    "type",
    "status",
]


class BusinessKnowledgeIndex:
    def __init__(self, corpus: KnowledgeCorpus, store: VectorStore | None = None):
        self.corpus = corpus
        self.store = (
            store
            if store is not None
            else NumpyVectorStore(
                embeddings=NumpyEmbeddings(),
                chunk_size=512,
                situate=False,
                excluded_metadata=list(EXCLUDED_METADATA),
            )
        )
        if self.store.situate:
            raise ValueError("Business knowledge retrieval requires situate=False")
        if len(self.store):
            raise ValueError("index requires an empty dedicated store")
        self.store.excluded_metadata = sorted(set(self.store.excluded_metadata + EXCLUDED_METADATA))
        self._built = False

    async def build(self) -> None:
        if self._built or len(self.store):
            raise ValueError("index already populated; create a fresh index to rebuild")
        items = []
        for document in self.corpus.documents:
            fields = document.model_dump(exclude={"text", "version"})
            metadata = KnowledgeMetadata(
                **fields,
                doc_version=document.version,
                filename=f"{document.doc_id}@{document.version}",
            )
            items.append({"text": document.text, "metadata": metadata.model_dump()})
        # Build failures propagate; query failures become explicit evidence errors.
        await self.store.add(items, situate=False)
        self._built = True

    async def search(
        self,
        query: str,
        top_k: int = 5,
        knowledge_types: list[KnowledgeType] | None = None,
        metric_ids: list[str] | None = None,
        include_deprecated: bool = False,
        *,
        doc_id: str | None = None,
        version: str | None = None,
        min_similarity: float = 0.0,
    ) -> KnowledgeSearchResult:
        if not query.strip():
            raise ValueError("query must not be empty")
        if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k < 1:
            raise ValueError("top_k must be a positive integer")
        if not math.isfinite(min_similarity) or not -1 <= min_similarity <= 1:
            raise ValueError("min_similarity must be between -1 and 1")
        if not self._built:
            raise ValueError("index must be built before search")
        filters: dict = {}
        if not include_deprecated:
            filters["status"] = "ACTIVE"
        if knowledge_types is not None:
            filters["knowledge_type"] = knowledge_types
        if metric_ids is not None:
            # Lumen list/list means ALL requested IDs, not OR.
            filters["metric_ids"] = metric_ids
        if doc_id is not None:
            filters["doc_id"] = doc_id
        if version is not None:
            filters["doc_version"] = version
        try:
            # Fetch all candidates before tie-breaking, so a tie at top_k is stable too.
            rows = await self.store.query(
                text=query,
                top_k=max(1, len(self.store)),
                filters=filters,
                threshold=min_similarity,
            )
            hits = []
            for row in rows:
                metadata = KnowledgeMetadata.model_validate(row["metadata"])
                hits.append(
                    KnowledgeHit(
                        text=row["text"],
                        similarity=round(float(row["similarity"]), 8),
                        metadata=metadata,
                        citation=citation_for(metadata, row["text"]),
                    )
                )
            hits.sort(
                key=lambda h: (
                    -h.similarity,
                    h.citation.doc_id,
                    h.citation.version,
                    h.citation.chunk_fingerprint,
                )
            )
        except Exception as exc:
            # Explicit diagnostic boundary; never conflate a broken store with no match.
            return KnowledgeSearchResult(status="ERROR", error_code=type(exc).__name__)
        hits = hits[: min(top_k, 50)]
        return KnowledgeSearchResult(status="SUCCESS" if hits else "NO_MATCH", hits=hits)

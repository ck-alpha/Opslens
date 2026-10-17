"""Deterministic knowledge snapshot with registry and renderer provenance."""

from pathlib import Path

from opslens.knowledge.citations import canonical_hash
from opslens.knowledge.manifest import load_manifest
from opslens.knowledge.metric_renderer import RENDERER_VERSION, MetricKnowledgeRenderer
from opslens.knowledge.models import KnowledgeDocument
from opslens.metrics import MetricRegistry


class KnowledgeCorpus:
    def __init__(self, registry: MetricRegistry, knowledge: Path):
        self.version, manual = load_manifest(knowledge)
        known_ids = {d.metric_id for d in registry.all()}
        if any(not set(d.metric_ids) <= known_ids for d in manual):
            raise ValueError("unknown metric reference in knowledge manifest")
        rendered = MetricKnowledgeRenderer().render(registry, include_deprecated=True)
        self._documents = sorted(manual + rendered, key=lambda d: (d.doc_id, d.version))
        self.registry_fingerprint = registry.fingerprint()
        self.fingerprint = canonical_hash(
            {
                "corpus_version": self.version,
                "renderer_version": RENDERER_VERSION,
                "metric_registry_fingerprint": self.registry_fingerprint,
                "documents": [d.model_dump(exclude={"text"}) for d in self._documents],
            }
        )

    @property
    def documents(self) -> list[KnowledgeDocument]:
        return [d.model_copy(deep=True) for d in self._documents]

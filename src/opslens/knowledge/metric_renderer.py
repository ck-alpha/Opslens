"""Render the registry, never a second manually maintained metric dictionary."""

import json

from opslens.knowledge.citations import content_hash
from opslens.knowledge.models import KnowledgeDocument
from opslens.metrics import MetricRegistry

RENDERER_VERSION = "1.0"


class MetricKnowledgeRenderer:
    def render(
        self, registry: MetricRegistry, *, include_deprecated: bool = False
    ) -> list[KnowledgeDocument]:
        fingerprint = registry.fingerprint()
        documents = []
        for definition in registry.all():
            if definition.status == "DEPRECATED" and not include_deprecated:
                continue
            fields = definition.model_dump()
            for key in (
                "aliases",
                "dependencies",
                "source_tables",
                "source_columns",
                "allowed_dimensions",
            ):
                fields[key].sort()
            fields["time_semantics"]["allowed_grains"].sort()
            for condition in fields["filters"]:
                if isinstance(condition["value"], list):
                    condition["value"].sort()
            fields["filters"].sort(key=lambda f: json.dumps(f, sort_keys=True))
            text = f"# {definition.name}\nSynthetic business knowledge\n\n" + "\n".join(
                f"{key}: {json.dumps(value, ensure_ascii=False, sort_keys=True)}"
                for key, value in fields.items()
            )
            documents.append(
                KnowledgeDocument(
                    doc_id=f"metric:{definition.metric_id}",
                    title=definition.name,
                    version=definition.version,
                    status=definition.status,
                    knowledge_type="METRIC_CONTRACT",
                    text=text,
                    tags=sorted(definition.aliases),
                    metric_ids=[definition.metric_id],
                    source_kind="METRIC_CONTRACT",
                    source_ref=f"metric:{definition.metric_id}@{definition.version}",
                    content_sha256=content_hash(text),
                    metric_registry_fingerprint=fingerprint,
                )
            )
        return documents

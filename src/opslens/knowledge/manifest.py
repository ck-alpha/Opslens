"""Load only explicitly declared local synthetic business documents."""

from pathlib import Path

from opslens.knowledge.citations import content_hash
from opslens.knowledge.models import KnowledgeDocument, KnowledgeManifest


def load_manifest(root: Path) -> tuple[str, list[KnowledgeDocument]]:
    manifest = KnowledgeManifest.model_validate_json((root / "manifest.json").read_text("utf-8"))
    documents = []
    seen: set[str] = set()
    for entry in sorted(manifest.documents, key=lambda e: (e.doc_id, e.version)):
        # Manual manifest deliberately permits one version per logical document.
        if entry.doc_id in seen:
            raise ValueError(f"duplicate document id / version or ACTIVE conflict: {entry.doc_id}")
        seen.add(entry.doc_id)
        path = (root / entry.relative_path).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file():
            raise ValueError(f"missing or escaping knowledge file: {entry.doc_id}")
        text = path.read_bytes().decode("utf-8")
        if not text.strip() or "Synthetic business knowledge" not in text:
            raise ValueError(f"empty or undeclared synthetic knowledge: {entry.doc_id}")
        fields = entry.model_dump(exclude={"relative_path"})
        fields["tags"] = sorted(set(entry.tags))
        fields["metric_ids"] = sorted(set(entry.metric_ids))
        documents.append(
            KnowledgeDocument(
                **fields,
                text=text,
                source_kind="FILE",
                source_ref=entry.relative_path,
                content_sha256=content_hash(text),
            )
        )
    return manifest.corpus_version, documents

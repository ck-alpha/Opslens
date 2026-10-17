"""Stable content identities; internal Lumen IDs are never citations."""

import hashlib
import json

from opslens.knowledge.models import KnowledgeCitation, KnowledgeMetadata


def canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chunk_fingerprint(doc_id: str, version: str, text: str) -> str:
    return canonical_hash([doc_id, version, " ".join(text.split())])


def citation_for(metadata: KnowledgeMetadata, text: str) -> KnowledgeCitation:
    return KnowledgeCitation(
        doc_id=metadata.doc_id,
        version=metadata.doc_version,
        title=metadata.title,
        knowledge_type=metadata.knowledge_type,
        source_ref=metadata.source_ref,
        chunk_fingerprint=chunk_fingerprint(metadata.doc_id, metadata.doc_version, text),
    )

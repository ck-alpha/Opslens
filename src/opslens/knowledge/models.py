"""Strict, versioned knowledge boundaries independent of vector IDs."""

from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated, Literal

from pydantic import Field, field_validator

from opslens.metrics.models import ContractModel, Identifier, Text, Version

KnowledgeType = Literal["METRIC_CONTRACT", "POLICY", "DATA_DICTIONARY", "ANALYSIS_GUIDELINE"]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
DocId = Annotated[str, Field(pattern=r"^(?:metric:)?[a-z][a-z0-9_]*$")]


def safe_relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or PureWindowsPath(value).drive
        or "\\" in value
        or ".." in path.parts
        or not path.parts
    ):
        raise ValueError("source path must be relative without traversal")
    return value


class KnowledgeManifestEntry(ContractModel):
    doc_id: Identifier
    title: Text
    version: Version
    status: Literal["ACTIVE", "DEPRECATED"]
    knowledge_type: Literal["POLICY", "DATA_DICTIONARY", "ANALYSIS_GUIDELINE"]
    relative_path: Text
    tags: list[Text]
    metric_ids: list[Identifier]

    @field_validator("relative_path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        safe_relative_path(value)
        if PurePosixPath(value).suffix not in (".md", ".txt"):
            raise ValueError("knowledge files must be Markdown or text")
        return value


class KnowledgeManifest(ContractModel):
    corpus_version: Version
    documents: list[KnowledgeManifestEntry] = Field(min_length=1)


class KnowledgeDocument(ContractModel):
    doc_id: DocId
    title: Text
    version: Version
    status: Literal["ACTIVE", "DEPRECATED"]
    knowledge_type: KnowledgeType
    text: Text
    tags: list[Text]
    metric_ids: list[Identifier]
    source_kind: Literal["FILE", "METRIC_CONTRACT"]
    source_ref: Text
    content_sha256: Digest
    metric_registry_fingerprint: Digest | None = None

    @field_validator("source_ref")
    @classmethod
    def validate_source(cls, value: str) -> str:
        return safe_relative_path(value)


class KnowledgeMetadata(ContractModel):
    doc_id: DocId
    doc_version: Version
    title: Text
    knowledge_type: KnowledgeType
    status: Literal["ACTIVE", "DEPRECATED"]
    tags: list[Text]
    metric_ids: list[Identifier]
    source_kind: Literal["FILE", "METRIC_CONTRACT"]
    source_ref: Text
    content_sha256: Digest
    metric_registry_fingerprint: Digest | None = None
    type: Literal["document"] = "document"
    filename: Text

    @field_validator("source_ref", "filename")
    @classmethod
    def validate_source(cls, value: str) -> str:
        return safe_relative_path(value)


class KnowledgeCitation(ContractModel):
    doc_id: DocId
    version: Version
    title: Text
    knowledge_type: KnowledgeType
    source_ref: Text
    chunk_fingerprint: Digest

    @field_validator("source_ref")
    @classmethod
    def validate_source(cls, value: str) -> str:
        return safe_relative_path(value)


class KnowledgeHit(ContractModel):
    text: Text
    similarity: float
    citation: KnowledgeCitation
    metadata: KnowledgeMetadata


class KnowledgeSearchResult(ContractModel):
    status: Literal["SUCCESS", "NO_MATCH", "ERROR"]
    hits: list[KnowledgeHit] = Field(default_factory=list)
    error_code: str | None = None

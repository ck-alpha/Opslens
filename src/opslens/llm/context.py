"""Auditable context from registry/schema/retrieval only; no evaluation imports."""

import json

from opslens.knowledge.models import KnowledgeSearchResult
from opslens.runtime.steps import Pipeline
from opslens.validation.intent import AnalysisIntent


def build_prompt(
    pipeline: Pipeline,
    question: str,
    intent: AnalysisIntent,
    knowledge: KnowledgeSearchResult,
    feedback: dict | None = None,
) -> str:
    registry = pipeline.validation.builder.registry
    definition = registry.get(intent.metric_id, intent.metric_version)
    if definition is None:
        raise ValueError("UNKNOWN_METRIC")
    contracts = {}

    def add(metric):
        contracts[metric.metric_id] = metric.model_dump(mode="json", exclude={"owner", "aliases"})
        for dep in metric.dependencies:
            if dep not in contracts:
                target = registry.active(dep)
                if target is None:
                    raise ValueError("MISSING_DEPENDENCY")
                add(target)

    add(definition)
    builder = pipeline.validation.builder
    schema = {}
    for col in builder.provenance.schema.columns:
        if f"{col.table}.{col.column}" not in pipeline.validation.sql_policy.denied_columns:
            schema.setdefault(col.table, {})[col.column] = col.data_type
    return json.dumps(
        dict(
            question=question,
            intent=intent.model_dump(mode="json"),
            contracts=contracts,
            allowed_schema=schema,
            dialect="duckdb",
            knowledge=[
                dict(citation=h.citation.model_dump(mode="json"), text=h.text[:600])
                for h in knowledge.hits[:2]
            ],
            repair_feedback=feedback,
        ),
        ensure_ascii=False,
        separators=(",", ":"),
    )


REPAIRABLE = frozenset(
    {"PARSE_ERROR", "UNKNOWN_COLUMN", "AMBIGUOUS_COLUMN_REFERENCE", "UNRESOLVED_COLUMN_REFERENCE"}
)


def can_repair(reasons: list[str]) -> bool:
    # Every reason must be benign. TABLE_NOT_ALLOWED is intentionally excluded:
    # an invented table cannot safely be distinguished from a forbidden source.
    return bool(reasons) and set(reasons) <= REPAIRABLE

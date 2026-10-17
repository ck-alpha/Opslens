import asyncio
from pathlib import Path

import pytest

from opslens.clarification import MetricClarificationService
from opslens.knowledge import BusinessKnowledgeIndex, KnowledgeCorpus
from opslens.metrics import MetricRegistry

ROOT = Path(__file__).parents[2]


@pytest.fixture
def service():
    registry = MetricRegistry.from_directory(ROOT / "data/metrics/definitions")
    index = BusinessKnowledgeIndex(KnowledgeCorpus(registry, ROOT / "data/knowledge"))
    asyncio.run(index.build())
    return MetricClarificationService(registry, index)


@pytest.mark.parametrize(
    ("query", "action", "ids"),
    [
        ("毛GMV", "PROCEED", []),
        ("净GMV", "PROCEED", []),
        ("GMV", "ASK_USER", ["gross_gmv", "net_gmv"]),
        ("退款率", "ASK_USER", ["refund_amount_rate", "refund_order_rate"]),
        ("客单价", "ASK_USER", ["gross_aov", "net_aov"]),
        ("哪个渠道表现最好", "ASK_USER", []),
        ("8月业务为什么变差", "ASK_USER", []),
    ],
)
def test_policy(service, query, action, ids):
    before = service.resolver.resolve(query)
    actual = asyncio.run(service.clarify(query))
    assert actual.action == action
    assert [o.metric_id for o in actual.options] == ids
    assert actual.metric_resolution == before
    assert (actual.question is None) == (action == "PROCEED")
    for option in actual.options:
        definition = service.registry.get(option.metric_id, option.version)
        assert option.description == definition.description
        assert option.time_column == definition.time_semantics.time_column
        assert option.citation.doc_id == f"metric:{option.metric_id}"
        assert option.citation.version == option.version
    if before.status == "UNKNOWN":
        assert actual.metric_resolution.metric_id is None
        assert actual.citations == []
    else:
        assert actual.citations


def test_deprecated_and_conflicts(service):
    result = asyncio.run(service.clarify("gross_gmv@0.9"))
    assert result.action == "ASK_USER" and result.reason_code == "DEPRECATED_METRIC_VERSION"
    assert [(o.version, o.status, o.time_column) for o in result.options] == [
        ("0.9", "DEPRECATED", "orders.created_at"),
        ("1.0", "ACTIVE", "orders.paid_at"),
    ]
    assert [c.version for c in result.citations] == ["0.9", "1.0"]
    result = asyncio.run(service.clarify("gross_gmv@0.9", "1.0"))
    assert result.action == "NEEDS_REVIEW" and result.options == []
    active = service.registry.get("gross_gmv")
    service.registry._definitions[("gross_gmv", "2.0")] = active.model_copy(
        update={"version": "2.0"}
    )
    result = asyncio.run(service.clarify("GMV"))
    assert result.action == "NEEDS_REVIEW" and result.question is None
    assert result.reason_code == "METRIC_REGISTRY_CONFLICT"


def test_evidence_failure_never_blocks_ambiguity(service, monkeypatch):
    async def broken(**kwargs):
        raise RuntimeError("sensitive local driver text")

    monkeypatch.setattr(service.index.store, "query", broken)
    actual = asyncio.run(service.clarify("GMV"))
    assert actual.action == "ASK_USER"
    assert {o.metric_id for o in actual.options} == {"gross_gmv", "net_gmv"}
    assert actual.citations == []
    assert actual.diagnostics == ["KNOWLEDGE_UNAVAILABLE:RuntimeError"]
    assert all(o.citation is None for o in actual.options)
    assert "sensitive" not in actual.model_dump_json()
    assert asyncio.run(service.clarify("毛GMV")).action == "PROCEED"
    assert asyncio.run(service.clarify("哪个渠道表现最好")).metric_resolution.status == "UNKNOWN"


def test_optional_evidence_and_stale_snapshot(service):
    bare = MetricClarificationService(service.registry)
    assert asyncio.run(bare.clarify("GMV")).action == "ASK_USER"
    definition = service.registry.get("gross_gmv")
    service.registry._definitions[("gross_gmv", "1.0")] = definition.model_copy(
        update={"description": "new definition"}
    )
    result = asyncio.run(service.clarify("GMV"))
    assert result.action == "ASK_USER" and result.citations == []
    assert result.diagnostics == ["KNOWLEDGE_UNAVAILABLE:REGISTRY_SNAPSHOT_MISMATCH"]

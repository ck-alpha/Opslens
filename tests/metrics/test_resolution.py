from pathlib import Path

import pytest

from opslens.metrics import MetricRegistry, MetricResolver
from opslens.metrics.normalization import normalize_text, phrase_spans

DEFINITIONS = Path(__file__).parents[2] / "data/metrics/definitions"


@pytest.fixture
def resolver():
    return MetricResolver(MetricRegistry.from_directory(DEFINITIONS))


@pytest.mark.parametrize(
    ("query", "status", "ids", "reason"),
    [
        ("gross_gmv", "RESOLVED", ["gross_gmv"], "EXACT_METRIC_ID"),
        ("毛GMV", "RESOLVED", ["gross_gmv"], "EXACT_CANONICAL_NAME"),
        ("8月毛 GMV是多少？", "RESOLVED", ["gross_gmv"], "LONGEST_ALIAS"),
        ("Gross GMV", "RESOLVED", ["gross_gmv"], "LONGEST_ALIAS"),
        ("ＧＭＶ", "AMBIGUOUS", ["gross_gmv", "net_gmv"], "AMBIGUOUS_ALIAS"),
        ("退款率", "AMBIGUOUS", ["refund_amount_rate", "refund_order_rate"], "AMBIGUOUS_ALIAS"),
        ("毛客单价", "RESOLVED", ["gross_aov"], "EXACT_CANONICAL_NAME"),
        ("净客单价", "RESOLVED", ["net_aov"], "EXACT_CANONICAL_NAME"),
        ("unknown", "UNKNOWN", [], "NO_METRIC_MATCH"),
        ("哪个渠道表现最好", "UNKNOWN", [], "NO_METRIC_MATCH"),
        ("8 月业务为什么变差", "UNKNOWN", [], "NO_METRIC_MATCH"),
        ("behavior xaov aov2 xgross_gmv", "UNKNOWN", [], "NO_METRIC_MATCH"),
        ("gross_gmv@0.9", "DEPRECATED", ["gross_gmv"], "DEPRECATED_VERSION"),
        ("gross_gmv v0.9", "DEPRECATED", ["gross_gmv"], "DEPRECATED_VERSION"),
        ("gross_gmv@9.0", "UNKNOWN", [], "VERSION_NOT_FOUND"),
        ("gross_gmv@0.9.2", "UNKNOWN", [], "INVALID_VERSION"),
        ("gross_gmv@no", "UNKNOWN", [], "INVALID_VERSION"),
        ("毛GMV和净GMV", "AMBIGUOUS", ["gross_gmv", "net_gmv"], "MULTIPLE_METRIC_MENTIONS"),
        ("gross_gmv 和净GMV", "AMBIGUOUS", ["gross_gmv", "net_gmv"], "MULTIPLE_METRIC_MENTIONS"),
        (
            "毛GMV与成交订单数",
            "AMBIGUOUS",
            ["gross_gmv", "paid_order_count"],
            "MULTIPLE_METRIC_MENTIONS",
        ),
        ("毛GMV、毛GMV", "RESOLVED", ["gross_gmv"], "EXACT_CANONICAL_NAME"),
    ],
)
def test_resolution(resolver, query, status, ids, reason):
    result = resolver.resolve(query)
    assert result.status == status
    assert [c.metric_id for c in result.candidates] == ids
    assert result.reason_code == reason
    assert result.model_dump() == resolver.resolve(query).model_dump()


def test_normalization(resolver):
    variants = ["GMV", "gmv", "ＧＭＶ", " GMV ", "“GMV”？"]
    assert len({normalize_text(v) for v in variants}) == 1
    assert all(resolver.resolve(v).status == "AMBIGUOUS" for v in variants)
    assert phrase_spans("aov", "behavior xaov aov2") == []
    assert resolver.resolve("Gross   GMV!").metric_id == "gross_gmv"


def test_versions_and_conflict(resolver):
    assert resolver.resolve("毛GMV", version="0.9").status == "DEPRECATED"
    assert resolver.resolve("毛GMV", version="1.0").version == "1.0"
    assert resolver.resolve("毛GMV", version="bad").reason_code == "INVALID_VERSION"
    result = resolver.resolve("gross_gmv@0.9", version="1.0")
    assert result.status == "CONFLICT" and result.reason_code == "VERSION_CONFLICT"
    # Deliberately corrupt a test-only registry to cover defense in depth.
    definition = resolver.registry.get("gross_gmv").model_copy(update={"version": "2.0"})
    resolver.registry._definitions[("gross_gmv", "2.0")] = definition
    result = resolver.resolve("gross_gmv")
    assert result.status == "CONFLICT" and result.reason_code == "MULTIPLE_ACTIVE_VERSIONS"


def test_canonical_priority_and_historical_only(resolver):
    gross = resolver.registry.get("gross_gmv")
    net = resolver.registry.get("paid_order_count")
    net.aliases.extend(["gross_gmv", "毛GMV"])
    custom = MetricResolver(MetricRegistry([gross, net]))
    assert custom.resolve("gross_gmv").metric_id == "gross_gmv"
    assert custom.resolve("毛GMV").metric_id == "gross_gmv"
    historical = MetricResolver(MetricRegistry([resolver.registry.get("gross_gmv", "0.9")]))
    result = historical.resolve("gross_gmv")
    assert result.status == "DEPRECATED" and result.version == "0.9"

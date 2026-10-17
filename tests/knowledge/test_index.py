import asyncio
import json
from collections import Counter

import pytest
from lumen.ai.embeddings import NumpyEmbeddings
from lumen.ai.vector_store import NumpyVectorStore

from opslens.knowledge import BusinessKnowledgeIndex


def search(index, query, **kwargs):
    return asyncio.run(index.search(query, **kwargs))


def test_real_store_and_chunks(index):
    assert isinstance(index.store, NumpyVectorStore)
    assert isinstance(index.store.embeddings, NumpyEmbeddings)
    assert index.store.chunk_size == 512
    assert index.store.situate is False
    assert index.store.chunk_tokenizer == "gpt-4o-mini"
    assert index.store.chunk_func_kwargs == {}
    assert len(index.store) > len(index.corpus.documents)
    counts = Counter(row["metadata"]["doc_id"] for row in index.store.filter_by({}))
    assert counts["data_dictionary"] > 1
    serialized = json.dumps(index.store.filter_by({}))
    assert "/home/" not in serialized
    assert "content_sha256" in serialized
    embedded = index.store._join_text_and_metadata(
        "content",
        {
            "title": "Title",
            "content_sha256": "SECRET_HASH",
            "source_ref": "SECRET_PATH",
            "metric_registry_fingerprint": "SECRET_REGISTRY",
            "llm_context": "SECRET_CONTEXT",
        },
    )
    assert "Title" in embedded and "SECRET" not in embedded


@pytest.mark.parametrize(
    ("query", "doc"),
    [
        ("gross_gmv", "metric:gross_gmv"),
        ("退款审批与时间归属政策", "refund_policy"),
        ("订单状态与成交政策", "order_status_policy"),
        ("affiliate 联盟", "channel_attribution"),
    ],
)
def test_canonical_retrieval(index, query, doc):
    result = search(index, query)
    assert result.status == "SUCCESS"
    assert doc in [h.citation.doc_id for h in result.hits]
    assert result == search(index, query)
    for hit in result.hits:
        assert hit.metadata.status == "ACTIVE"
        assert hit.citation.version == hit.metadata.doc_version
        assert len(hit.citation.chunk_fingerprint) == 64
        assert not hit.citation.source_ref.startswith("/")


def test_filters_reach_lumen(index, monkeypatch):
    original = index.store.query
    received = []

    async def query(*args, **kwargs):
        received.append(kwargs["filters"])
        return await original(*args, **kwargs)

    monkeypatch.setattr(index.store, "query", query)
    result = search(
        index, "refund", knowledge_types=["POLICY"], metric_ids=["approved_refund_amount"]
    )
    assert received == [
        {"status": "ACTIVE", "knowledge_type": ["POLICY"], "metric_ids": ["approved_refund_amount"]}
    ]
    assert {h.citation.doc_id for h in result.hits} == {"refund_policy"}
    assert search(index, "refund", metric_ids=["unknown"]).status == "NO_MATCH"
    assert search(index, "refund", knowledge_types=[]).status == "NO_MATCH"
    assert all(
        set(["net_gmv", "gross_gmv"]) <= set(h.metadata.metric_ids)
        for h in search(index, "orders", metric_ids=["net_gmv", "gross_gmv"]).hits
    )


def test_deprecated(index):
    assert search(index, "gross_gmv", doc_id="metric:gross_gmv", version="0.9").status == "NO_MATCH"
    result = search(
        index, "gross_gmv", doc_id="metric:gross_gmv", version="0.9", include_deprecated=True
    )
    assert result.hits and all(h.citation.version == "0.9" for h in result.hits)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"query": " "},
        {"query": "x", "top_k": 0},
        {"query": "x", "top_k": True},
        {"query": "x", "min_similarity": float("nan")},
    ],
)
def test_bad_search(index, kwargs):
    with pytest.raises(ValueError):
        asyncio.run(index.search(**kwargs))


def test_errors_and_ties(index, monkeypatch):
    async def broken(**kwargs):
        raise RuntimeError("/home/private/never-expose")

    monkeypatch.setattr(index.store, "query", broken)
    result = search(index, "GMV")
    assert result.status == "ERROR" and result.error_code == "RuntimeError"
    assert "/home/" not in result.model_dump_json()
    rows = index.store.filter_by({"status": "ACTIVE"})
    rows = [row | {"similarity": 0.5} for row in rows]

    async def tied(**kwargs):
        assert kwargs["top_k"] == len(index.store)
        rows.reverse()
        return rows * 5

    monkeypatch.setattr(index.store, "query", tied)
    assert search(index, "query", top_k=1) == search(index, "query", top_k=1)
    assert len(search(index, "query", top_k=999).hits) == 50


def test_build_guards(corpus):
    index = BusinessKnowledgeIndex(corpus)
    with pytest.raises(ValueError, match="built"):
        search(index, "GMV")
    asyncio.run(index.build())
    with pytest.raises(ValueError, match="populated"):
        asyncio.run(index.build())
    with pytest.raises(ValueError, match="empty"):
        BusinessKnowledgeIndex(corpus, index.store)
    with pytest.raises(ValueError, match="situate"):
        BusinessKnowledgeIndex(corpus, NumpyVectorStore(situate=True))

import asyncio
from pathlib import Path

import pytest

from opslens.knowledge import BusinessKnowledgeIndex, KnowledgeCorpus
from opslens.metrics import MetricRegistry

ROOT = Path(__file__).parents[2]


@pytest.fixture
def registry():
    return MetricRegistry.from_directory(ROOT / "data/metrics/definitions")


@pytest.fixture
def corpus(registry):
    return KnowledgeCorpus(registry, ROOT / "data/knowledge")


@pytest.fixture(scope="module")
def index():
    corpus = KnowledgeCorpus(
        MetricRegistry.from_directory(ROOT / "data/metrics/definitions"), ROOT / "data/knowledge"
    )
    index = BusinessKnowledgeIndex(corpus)
    asyncio.run(index.build())
    return index

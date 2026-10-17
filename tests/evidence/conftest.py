from datetime import datetime, timezone
from pathlib import Path

import pytest

from opslens.evidence.builder import EvidenceBuilder
from opslens.evidence.service import EvidenceExecutionService
from opslens.knowledge import KnowledgeCorpus
from opslens.metrics import MetricRegistry
from opslens.sql.policy import SQLPolicyConfig
from opslens.sql.schema import DatabaseSchemaSnapshot
from opslens.sql.service import GovernedSQLService

ROOT = Path(__file__).parents[2]


@pytest.fixture
def service(demo_db):
    policy = SQLPolicyConfig.load(ROOT / "data/sql_policy/default.v1.json")
    schema = DatabaseSchemaSnapshot.load(demo_db, policy)
    registry = MetricRegistry.from_directory(ROOT / "data/metrics/definitions")
    corpus = KnowledgeCorpus(registry, ROOT / "data/knowledge")
    return EvidenceExecutionService(
        GovernedSQLService(demo_db, policy),
        EvidenceBuilder(
            demo_db,
            schema,
            registry,
            corpus,
            clock=lambda: datetime(2026, 9, 11, tzinfo=timezone.utc),
        ),
    )


@pytest.fixture
def record(service):
    return service.execute_with_evidence("SELECT COUNT(*) AS n FROM orders").evidence

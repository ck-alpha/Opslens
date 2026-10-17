"""Composition adapters for the existing governed and verified pipeline."""

from pathlib import Path

from opslens.evidence.builder import EvidenceBuilder
from opslens.evidence.hashing import digest
from opslens.evidence.service import EvidenceExecutionService
from opslens.knowledge import KnowledgeCorpus
from opslens.metrics import MetricRegistry
from opslens.runtime.lumen_baseline import LUMEN_BASELINE_COMMIT
from opslens.runtime.policy import RuntimePolicy
from opslens.sql.policy import SQLPolicyConfig
from opslens.sql.schema import DatabaseSchemaSnapshot
from opslens.sql.service import GovernedSQLService
from opslens.validation.oracle import DuckDBMetricOracle
from opslens.validation.policy import ValidationPolicy
from opslens.validation.service import ValidationService

RUNTIME_VERSION = WORKFLOW_VERSION = "m7.0"


class Pipeline:
    def __init__(self, evidence: EvidenceExecutionService, validation: ValidationService):
        self.evidence = evidence
        self.validation = validation

    def context_fingerprint(self, policy: RuntimePolicy) -> str:
        return digest(
            {
                "runtime_version": RUNTIME_VERSION,
                "workflow_version": WORKFLOW_VERSION,
                "runtime_policy": policy.fingerprint(),
                "sql_policy": self.validation.sql_policy.fingerprint(),
                "metric_registry": self.validation.builder.registry.fingerprint(),
                "knowledge_corpus": self.validation.builder.corpus.fingerprint,
                "validation_policy": self.validation.policy.fingerprint(),
                "lumen_sha": LUMEN_BASELINE_COMMIT,
            }
        )

    @classmethod
    def build(cls, analysis_db: Path, data: Path = Path("data")) -> "Pipeline":
        policy = SQLPolicyConfig.load(data / "sql_policy/default.v1.json")
        registry = MetricRegistry.from_directory(data / "metrics/definitions")
        builder = EvidenceBuilder(
            analysis_db,
            DatabaseSchemaSnapshot.load(analysis_db, policy),
            registry,
            KnowledgeCorpus(registry, data / "knowledge"),
        )
        return cls(
            EvidenceExecutionService(GovernedSQLService(analysis_db, policy), builder),
            ValidationService(
                builder,
                policy,
                ValidationPolicy.load(data / "validation/default.v1.json"),
                DuckDBMetricOracle(analysis_db, registry),
            ),
        )

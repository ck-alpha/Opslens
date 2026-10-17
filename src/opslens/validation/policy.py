"""Versioned authoritative validation policy."""

from pathlib import Path

from pydantic import Field

from opslens.evidence.hashing import digest
from opslens.metrics.models import ContractModel


class ValidationPolicy(ContractModel):
    policy_id: str = "opslens_validation"
    version: str = "1.0"
    require_complete_evidence_for_metric_pass: bool = True
    require_metric_oracle_for_pass: bool = True
    require_current_registry_match: bool = True
    require_current_corpus_match: bool = True
    require_current_policy_match: bool = True
    require_snapshot_match: bool = True
    allow_deprecated_metrics_by_default: bool = False
    max_report_rows: int = Field(default=20, ge=1, le=1000)
    ratio_tolerance: float = Field(default=1e-9, ge=0, le=1e-6)

    def fingerprint(self) -> str:
        return digest(self.model_dump(mode="json"))

    @classmethod
    def load(cls, path: Path) -> "ValidationPolicy":
        return cls.model_validate_json(path.read_text("utf-8"))

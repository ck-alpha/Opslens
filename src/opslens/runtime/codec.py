"""Single artifact registry with strict JSON and evidence and validation integrity helpers."""

import json
from typing import Any

from pydantic import BaseModel, ValidationError

from opslens.evidence.builder import content_fingerprint
from opslens.evidence.hashing import digest
from opslens.evidence.models import EvidenceRecord
from opslens.reporting.builder import report_fingerprint
from opslens.reporting.models import VerifiedReport
from opslens.runtime.failures import RuntimeFault
from opslens.sql.models import GovernedSQLResult
from opslens.validation.models import ValidationRecord
from opslens.validation.status import validation_fingerprint

REGISTRY: dict[str, type[BaseModel]] = {
    "governed_sql_result": GovernedSQLResult,
    "evidence_record": EvidenceRecord,
    "validation_record": ValidationRecord,
    "verified_report": VerifiedReport,
}
SCHEMA_VERSION = "1.0"


def envelope_hash(kind: str, version: str, payload: dict[str, Any]) -> str:
    return digest({"artifact_type": kind, "schema_version": version, "payload": payload})


def decode(kind: str, version: str, payload: dict[str, Any], fingerprint: str) -> BaseModel:
    if kind not in REGISTRY or version != SCHEMA_VERSION:
        raise RuntimeFault("ARTIFACT_SCHEMA_ERROR")
    if envelope_hash(kind, version, payload) != fingerprint:
        raise RuntimeFault("ARTIFACT_INTEGRITY_ERROR")
    try:
        model = REGISTRY[kind].model_validate_json(json.dumps(payload, allow_nan=False))
    except (ValidationError, ValueError, TypeError):
        raise RuntimeFault("ARTIFACT_SCHEMA_ERROR") from None
    valid = True
    if isinstance(model, EvidenceRecord):
        valid = content_fingerprint(model) == model.content_fingerprint
    elif isinstance(model, ValidationRecord):
        valid = validation_fingerprint(model) == model.validation_fingerprint
    elif isinstance(model, VerifiedReport):
        valid = report_fingerprint(model) == model.report_fingerprint
    if not valid:
        raise RuntimeFault("ARTIFACT_INTEGRITY_ERROR")
    return model


def encode(kind: str, model: BaseModel) -> dict[str, Any]:
    payload = model.model_dump(mode="json", by_alias=True)
    fp = envelope_hash(kind, SCHEMA_VERSION, payload)
    decode(kind, SCHEMA_VERSION, payload, fp)
    return dict(
        artifact_type=kind, schema_version=SCHEMA_VERSION, payload=payload, content_fingerprint=fp
    )

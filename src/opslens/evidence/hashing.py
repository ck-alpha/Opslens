"""Canonical JSON fingerprints; result hashes preserve exact serialized row order."""

import hashlib
import json

from opslens.sql.models import GovernedSQLResult


def canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def result_hash(result: GovernedSQLResult) -> str:
    if result.status != "EXECUTED" or result.row_count != len(result.rows):
        raise ValueError("INVALID_EXECUTION_RESULT")
    if any(len(row) != len(result.columns) for row in result.rows):
        raise ValueError("INVALID_RESULT_ROW_WIDTH")
    return digest(result.model_dump(mode="json", include={"columns", "rows", "row_count"}))

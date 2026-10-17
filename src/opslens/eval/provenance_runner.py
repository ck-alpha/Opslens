"""Independent input loading and adversarial evidence-ledger test helpers."""

import json
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from opslens.evidence.hashing import canonical_json
from opslens.evidence.ledger import EvidenceLedger

T = TypeVar("T", bound=BaseModel)


def load_cases(path: Path, model: type[T]) -> list[T]:
    cases = [model.model_validate_json(line) for line in path.read_text("utf-8").splitlines()]
    ids = [getattr(c, "case_id") for c in cases]
    if not cases or len(ids) != len(set(ids)):
        raise ValueError("EMPTY_OR_DUPLICATE_CASES")
    return cases


def tamper_checks(ledger: EvidenceLedger) -> dict[str, bool]:
    checks = {}
    for kind in (
        "evidence",
        "timestamp",
        "previous_hash",
        "entry_hash",
        "reorder",
        "delete_middle",
    ):
        data = json.loads(ledger.export())
        entries = data["entries"]
        if len(entries) < 3:
            raise ValueError("TAMPER_CHECK_NEEDS_THREE_ENTRIES")
        if kind == "evidence":
            entries[1]["evidence"]["result"]["row_count"] += 1
        elif kind == "timestamp":
            entries[1]["evidence"]["recorded_at"] = "2000-01-01T00:00:00Z"
        elif kind == "previous_hash":
            entries[1]["previous_entry_hash"] = "0" * 64
        elif kind == "entry_hash":
            entries[1]["entry_hash"] = "0" * 64
        elif kind == "reorder":
            entries[0], entries[1] = entries[1], entries[0]
        else:
            del entries[1]
        try:
            EvidenceLedger.load_and_verify(canonical_json(data))
            checks[kind] = False
        except ValueError:
            checks[kind] = True
    return checks

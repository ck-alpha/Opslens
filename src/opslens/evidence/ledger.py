"""In-process append-only hash chain with defensive copies and verified JSON export."""

import json

from opslens.evidence.builder import content_fingerprint
from opslens.evidence.hashing import canonical_json, digest
from opslens.evidence.models import EvidenceRecord, LedgerEntry


def entry_hash(entry: LedgerEntry) -> str:
    return digest(
        {
            "sequence": entry.sequence,
            "previous_entry_hash": entry.previous_entry_hash,
            "content_fingerprint": entry.evidence.content_fingerprint,
            "recorded_at": entry.evidence.model_dump(mode="json")["recorded_at"],
        }
    )


class EvidenceLedger:
    def __init__(self) -> None:
        self._entries: list[LedgerEntry] = []

    def entries(self) -> list[LedgerEntry]:
        return [e.model_copy(deep=True) for e in self._entries]

    @property
    def head_hash(self) -> str | None:
        return self._entries[-1].entry_hash if self._entries else None

    def append(self, record: EvidenceRecord) -> LedgerEntry:
        record = EvidenceRecord.model_validate_json(record.model_dump_json(by_alias=True))
        if content_fingerprint(record) != record.content_fingerprint:
            raise ValueError("EVIDENCE_CONTENT_FINGERPRINT_MISMATCH")
        entry = LedgerEntry(
            sequence=len(self._entries) + 1,
            previous_entry_hash=self.head_hash,
            evidence=record,
            entry_hash="",
        )
        entry = entry.model_copy(update={"entry_hash": entry_hash(entry)}, deep=True)
        self._entries.append(entry)
        return entry.model_copy(deep=True)

    def verify(self) -> bool:
        previous = None
        for sequence, entry in enumerate(self._entries, 1):
            if (
                entry.sequence != sequence
                or entry.previous_entry_hash != previous
                or content_fingerprint(entry.evidence) != entry.evidence.content_fingerprint
                or entry_hash(entry) != entry.entry_hash
            ):
                return False
            previous = entry.entry_hash
        return True

    def export(self) -> str:
        return canonical_json(
            {
                "ledger_version": "m5.0",
                "head_hash": self.head_hash,
                "entries": [e.model_dump(mode="json", by_alias=True) for e in self._entries],
            }
        )

    @classmethod
    def load_and_verify(cls, payload: str) -> "EvidenceLedger":
        data = json.loads(payload)
        if (
            not isinstance(data, dict)
            or set(data) != {"ledger_version", "head_hash", "entries"}
            or data["ledger_version"] != "m5.0"
            or not isinstance(data["entries"], list)
        ):
            raise ValueError("INVALID_LEDGER_FORMAT")
        ledger = cls()
        ledger._entries = [
            LedgerEntry.model_validate_json(canonical_json(e)) for e in data["entries"]
        ]
        if not ledger.verify() or ledger.head_hash != data["head_hash"]:
            raise ValueError("LEDGER_VERIFICATION_FAILED")
        return ledger

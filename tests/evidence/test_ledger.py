import json

import pytest

from opslens.eval.provenance_runner import tamper_checks
from opslens.evidence.ledger import EvidenceLedger


def test_empty_and_round_trip(record):
    ledger = EvidenceLedger()
    assert ledger.head_hash is None and ledger.entries() == [] and ledger.verify()
    assert EvidenceLedger.load_and_verify(ledger.export()).verify()
    previous = None
    for sequence in range(1, 4):
        entry = ledger.append(record)
        assert entry.sequence == sequence and entry.previous_entry_hash == previous
        previous = entry.entry_hash
        assert ledger.head_hash == previous and ledger.verify()
    loaded = EvidenceLedger.load_and_verify(ledger.export())
    assert loaded.export() == ledger.export()
    assert all(tamper_checks(ledger).values())
    assert not hasattr(ledger, "update") and not hasattr(ledger, "delete")


def test_defensive_copies(record):
    ledger = EvidenceLedger()
    returned = ledger.append(record)
    record.result.columns.append("outside")
    returned.evidence.result.columns.append("returned")
    entries = ledger.entries()
    entries[0].evidence.result.columns.append("copy")
    entries.clear()
    assert ledger.entries()[0].evidence.result.columns == ["n"] and ledger.verify()


def test_invalid_record_and_head(record):
    ledger = EvidenceLedger()
    with pytest.raises(ValueError, match="EVIDENCE_CONTENT_FINGERPRINT_MISMATCH"):
        ledger.append(record.model_copy(update={"policy_version": "99.0"}))
    ledger.append(record)
    data = json.loads(ledger.export())
    data["head_hash"] = "0" * 64
    with pytest.raises(ValueError):
        EvidenceLedger.load_and_verify(json.dumps(data))
    for payload in ["[]", "{}", "null", '{"ledger_version":"wrong"}']:
        with pytest.raises(ValueError):
            EvidenceLedger.load_and_verify(payload)

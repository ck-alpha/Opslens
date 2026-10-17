"""Logical content identity, captured after execution on synthetic DuckDB."""

import json
from pathlib import Path

from opslens.demo.config import TABLES
from opslens.demo.generator import fingerprints
from opslens.evidence.hashing import digest
from opslens.provenance.models import DataSnapshot
from opslens.sql.schema import readonly_connection


def capture_snapshot(db: Path, referenced_tables: list[str]) -> DataSnapshot:
    tables = sorted(set(referenced_tables))
    if not set(tables) <= {f"main.{t}" for t in TABLES}:
        raise ValueError("UNKNOWN_SNAPSHOT_TABLE")
    with readonly_connection(db) as connection:
        connection.execute("BEGIN TRANSACTION")
        metadata = {
            k: json.loads(v)
            for k, v in connection.execute("SELECT key, value FROM _demo_metadata").fetchall()
        }
        hashes = fingerprints(connection)
        counts = {
            t: connection.execute(f"SELECT COUNT(*) FROM {t}").fetchall()[0][0] for t in tables
        }
        connection.execute("COMMIT")
    fields = ("generator_version", "schema_version", "seed", "timezone", "synthetic")
    if not set(fields) <= metadata.keys():
        raise ValueError("INCOMPLETE_DEMO_METADATA")
    snapshot = DataSnapshot(
        **{k: metadata[k] for k in fields},
        referenced_tables=tables,
        table_row_counts=counts,
        table_fingerprints={t: hashes[t.removeprefix("main.")] for t in tables},
        snapshot_fingerprint="",
    )
    return snapshot.model_copy(update={"snapshot_fingerprint": snapshot_fingerprint(snapshot)})


def snapshot_fingerprint(snapshot: DataSnapshot) -> str:
    """Public canonical snapshot identity."""
    return digest(snapshot.model_dump(mode="json", exclude={"snapshot_fingerprint"}))

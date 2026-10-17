"""Session lock held on the very connection used for runtime mutations."""

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from opslens.runtime.failures import RuntimeFault


def lock_key(run_id: str) -> int:
    # Lock identity must not change when configurable policy changes on resume.
    # Namespace is fixed protocol identity; policy fingerprint still detects drift.
    raw = hashlib.sha256(b"opslens/m7/7001/run/" + UUID(run_id).bytes).digest()[:8]
    return int.from_bytes(raw, "big", signed=True)


@contextmanager
def claim_run(engine: Engine, run_id: str) -> Iterator[Connection]:
    key = lock_key(run_id)
    with engine.connect() as connection:
        acquired = connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
        connection.commit()
        if not acquired:
            raise RuntimeFault("RUN_LOCK_CONFLICT")
        try:
            yield connection
        finally:
            # A broken session must never be transparently reconnected and allowed
            # to continue executing without its lock. NullPool closes it on exit.
            if not connection.invalidated:
                connection.rollback()
                connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                connection.commit()

"""Private execution boundary: fresh read-only connection and active cancellation."""

import math
import threading
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

import duckdb
from pydantic import JsonValue

from opslens.sql.models import GovernedSQLResult, PolicyDecision
from opslens.sql.schema import readonly_connection


def serialize_cell(value: object) -> JsonValue:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, (list, tuple)):
        return [serialize_cell(v) for v in value]
    if isinstance(value, dict) and all(isinstance(k, str) for k in value):
        return {k: serialize_cell(v) for k, v in value.items()}
    raise ValueError("Unsupported result value")


class DuckDBGovernedExecutor:
    def __init__(self, db: Path):
        self._db = db

    def _execute(
        self, decision: PolicyDecision, timeout_seconds: float, max_rows: int
    ) -> GovernedSQLResult:
        if decision.decision != "ALLOW" or not decision.effective_sql:
            raise ValueError("Executor requires an allowed decision")
        start = time.monotonic()
        timed_out = threading.Event()
        finished = threading.Event()
        connection = None
        watcher = None
        values: dict = {}
        try:
            connection = readonly_connection(self._db)
            active_connection = connection

            def cancel() -> None:
                if not finished.wait(timeout_seconds):
                    timed_out.set()
                    # Repeated interrupt covers the narrow pre-execute scheduling race.
                    while not finished.is_set():
                        active_connection.interrupt()
                        finished.wait(0.005)

            watcher = threading.Thread(target=cancel, name="opslens-sql-cancel")
            watcher.start()
            cursor = connection.execute(decision.effective_sql)
            raw_rows = cursor.fetchmany(max_rows + 1)
            if len(raw_rows) > max_rows:
                raise ValueError("Result budget invariant violated")
            values = {
                "status": "EXECUTED",
                "columns": [c[0] for c in cursor.description],
                "rows": cast(list[list[JsonValue]], serialize_cell(raw_rows)),
                "row_count": len(raw_rows),
            }
        except duckdb.Error as exc:
            values = {
                "status": "ERROR",
                "error_code": "DATABASE_ERROR",
                "error_type": type(exc).__name__,
            }
        except (ValueError, TypeError, OverflowError) as exc:
            values = {
                "status": "ERROR",
                "error_code": "SERIALIZATION_ERROR",
                "error_type": type(exc).__name__,
            }
        except OSError as exc:
            values = {
                "status": "ERROR",
                "error_code": "DATABASE_ERROR",
                "error_type": type(exc).__name__,
            }
        finally:
            finished.set()
            if watcher is not None:
                watcher.join()
            if connection is not None:
                connection.close()
        if timed_out.is_set():
            values = {"status": "TIMEOUT", "error_code": "QUERY_TIMEOUT"}
        return GovernedSQLResult(
            decision=decision, elapsed_ms=(time.monotonic() - start) * 1000, **values
        )

"""Read-only catalog validation; expressions are never executed or compiled."""

from pathlib import Path

import duckdb

from opslens.metrics.models import SchemaValidationResult
from opslens.metrics.registry import MetricRegistry


def validate_schema(registry: MetricRegistry, db: Path) -> SchemaValidationResult:
    errors = []
    try:
        registry.validate()
    except ValueError as exc:
        errors.append(str(exc))
    with duckdb.connect(str(db), read_only=True, config={"enable_external_access": False}) as conn:
        columns = {
            f"{table}.{column}"
            for table, column in conn.execute(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_catalog = current_database() AND table_schema = 'main'"
            ).fetchall()
        }
    tables = {column.split(".")[0] for column in columns}
    for definition in registry.all():
        prefix = f"{definition.metric_id}@{definition.version}"
        for table in definition.source_tables:
            if table not in tables:
                errors.append(f"{prefix}: missing source table {table}")
        references = [("source", c) for c in definition.source_columns]
        references += [("time", definition.time_semantics.time_column)]
        references += [("filter", f.column) for f in definition.filters]
        references += [("dimension", c) for c in definition.allowed_dimensions]
        for kind, column in references:
            if column not in columns:
                errors.append(f"{prefix}: missing {kind} column {column}")
            if kind != "dimension" and column.split(".")[0] not in definition.source_tables:
                errors.append(f"{prefix}: undeclared source table for {kind} column {column}")
        for kind, column in references:
            if kind in ("time", "filter") and column not in definition.source_columns:
                errors.append(f"{prefix}: {kind} column absent from source_columns: {column}")
    return SchemaValidationResult(passed=not errors, errors=sorted(set(errors)))

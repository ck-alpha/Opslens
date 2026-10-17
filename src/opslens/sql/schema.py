"""Trusted read-only introspection, separate from candidate SQL."""

from pathlib import Path

import duckdb

from opslens.sql.models import StrictModel
from opslens.sql.policy import SQLPolicyConfig


def readonly_connection(path: Path) -> duckdb.DuckDBPyConnection:
    if not path.is_file():
        raise FileNotFoundError("Governed database must already exist")
    return duckdb.connect(
        str(path),
        read_only=True,
        config={
            "enable_external_access": False,
            "autoload_known_extensions": False,
            "autoinstall_known_extensions": False,
            "threads": 1,
        },
    )


class SchemaColumn(StrictModel):
    table: str
    column: str
    data_type: str
    ordinal_position: int


class DatabaseSchemaSnapshot(StrictModel):
    columns: tuple[SchemaColumn, ...]

    @classmethod
    def load(cls, db: Path, policy: SQLPolicyConfig) -> "DatabaseSchemaSnapshot":
        with readonly_connection(db) as connection:
            rows = connection.execute(
                "SELECT table_name, column_name, data_type, ordinal_position "
                "FROM information_schema.columns WHERE table_catalog = current_database() "
                "AND table_schema = 'main' ORDER BY table_name, ordinal_position"
            ).fetchall()
            # A view may hide forbidden tables/functions: only actual base tables are eligible.
            base_tables = {
                r[0]
                for r in connection.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_catalog = current_database() AND table_schema = 'main' "
                    "AND table_type = 'BASE TABLE'"
                ).fetchall()
            }
        snapshot = cls(
            columns=tuple(
                SchemaColumn(table=t, column=c, data_type=d, ordinal_position=o)
                for t, c, d, o in rows
                if t in policy.allowed_tables and t in base_tables
            )
        )
        validate_policy_against_schema(policy, snapshot)
        return snapshot

    def sqlglot_schema(self) -> dict[str, dict[str, dict[str, str]]]:
        tables: dict[str, dict[str, str]] = {}
        for col in self.columns:
            tables.setdefault(col.table, {})[col.column] = col.data_type
        return {"main": tables}


def validate_policy_against_schema(policy: SQLPolicyConfig, schema: DatabaseSchemaSnapshot) -> None:
    tables = {c.table for c in schema.columns}
    columns = {f"{c.table}.{c.column}" for c in schema.columns}
    if not set(policy.allowed_tables) <= tables:
        raise ValueError("Policy allows nonexistent or nonphysical tables")
    if not set(policy.denied_columns) <= columns:
        raise ValueError("Policy denies nonexistent columns")

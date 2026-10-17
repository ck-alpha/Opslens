"""Adapter over SQLGlot public lineage; policy reference lists are never column lineage."""

import hashlib
from typing import Literal

from sqlglot import exp, parse_one
from sqlglot.errors import SqlglotError
from sqlglot.lineage import Node, lineage
from sqlglot.optimizer.scope import Scope, build_scope

from opslens.evidence.hashing import digest
from opslens.provenance.models import (
    OutputColumnProvenance,
    PhysicalColumnRef,
    QueryProvenance,
)
from opslens.sql.models import GovernedSQLResult
from opslens.sql.schema import DatabaseSchemaSnapshot


class UnresolvedLineage(ValueError):
    pass


class Resolver:
    def __init__(self, root: Scope, schema: DatabaseSchemaSnapshot):
        self.root = root
        self.schema = schema
        self.scopes = list(root.traverse())
        self.known = {f"main.{c.table}.{c.column}" for c in schema.columns}
        self.tables = {f"main.{c.table}" for c in schema.columns}

    def table(self, table: exp.Table) -> str:
        name = f"{table.db or 'main'}.{table.name}"
        if table.catalog or name not in self.tables:
            raise UnresolvedLineage("UNKNOWN_LINEAGE_TABLE")
        return name

    def ref(self, table: exp.Table, column: str) -> PhysicalColumnRef:
        name = self.table(table)
        if f"{name}.{column}" not in self.known:
            raise UnresolvedLineage("UNKNOWN_LINEAGE_COLUMN")
        return PhysicalColumnRef(schema="main", table=table.name, column=column)

    def rowset(self, scope: Scope) -> set[str]:
        tables: set[str] = set()
        for _, source in scope.selected_sources.values():
            if isinstance(source, exp.Table):
                tables.add(self.table(source))
            elif isinstance(source, Scope):
                tables.update(self.rowset(source))
            else:
                raise UnresolvedLineage("UNRESOLVED_LINEAGE_NODE")
        for branch in scope.union_scopes:
            tables.update(self.rowset(branch))
        return tables

    def node(
        self, root: Node
    ) -> tuple[
        list[PhysicalColumnRef], list[str], Literal["COLUMN_VALUE", "ROWSET", "CONSTANT", "MIXED"]
    ]:
        columns: dict[str, PhysicalColumnRef] = {}
        row_tables: set[str] = set()
        row_dependency = False
        for node in root.walk():
            expression = node.expression
            if isinstance(expression, exp.Table):
                col = parse_one(node.name, read="duckdb")
                if not isinstance(col, exp.Column):
                    raise UnresolvedLineage("UNRESOLVED_LINEAGE_NODE")
                ref = self.ref(expression, col.name)
                columns[ref.canonical] = ref
                continue
            if isinstance(expression, exp.Placeholder):
                raise UnresolvedLineage("UNRESOLVED_LINEAGE_NODE")
            # COUNT(*) and aggregates of literals still consume rows, including through CTEs.
            aggregates = list(expression.find_all(exp.AggFunc))
            if any(
                a.find(exp.Star) is not None or not list(a.find_all(exp.Column)) for a in aggregates
            ):
                scope = next((s for s in self.scopes if s.expression is node.source), None)
                if scope is None:
                    raise UnresolvedLineage("UNRESOLVED_LINEAGE_NODE")
                row_tables.update(self.rowset(scope))
                row_dependency = True
            if not node.downstream and list(expression.find_all(exp.Column)):
                raise UnresolvedLineage("UNRESOLVED_LINEAGE_NODE")
        tables = row_tables | {f"{c.schema_name}.{c.table}" for c in columns.values()}
        kind: Literal["COLUMN_VALUE", "ROWSET", "CONSTANT", "MIXED"] = (
            ("MIXED" if columns else "ROWSET")
            if row_dependency
            else ("COLUMN_VALUE" if columns else "CONSTANT")
        )
        return [columns[k] for k in sorted(columns)], sorted(tables), kind

    def column(self, scope: Scope, column: exp.Column) -> list[PhysicalColumnRef]:
        source = scope.sources.get(column.table)
        if isinstance(source, exp.Table):
            return [self.ref(source, column.name)]
        if isinstance(source, Scope):
            node = lineage(
                column.name, source.expression, scope=source, dialect="duckdb", trim_selects=False
            )
            return self.node(node)[0]
        raise UnresolvedLineage("UNRESOLVED_LINEAGE_NODE")


class ProvenanceBuilder:
    def __init__(self, schema: DatabaseSchemaSnapshot):
        self.schema = schema

    def build(self, result: GovernedSQLResult) -> QueryProvenance:
        from opslens.provenance.context import build_context

        decision = result.decision
        sql = decision.effective_sql
        if not sql or hashlib.sha256(sql.encode()).hexdigest() != decision.query_fingerprint:
            raise ValueError("QUERY_FINGERPRINT_MISMATCH")
        if result.status != "EXECUTED" or decision.decision != "ALLOW":
            raise ValueError("EXECUTED_RESULT_REQUIRED")
        assert decision.query_fingerprint is not None
        outputs: list[OutputColumnProvenance] = []
        context = []
        tables: list[str] = []
        reasons: list[str] = []
        expressions: list[exp.Expr] = []
        try:
            tree = parse_one(sql, read="duckdb")
            if not isinstance(tree, exp.Query):
                raise UnresolvedLineage("UNSUPPORTED_LINEAGE_STRUCTURE")
            expressions = list(tree.selects)
            names = [e.alias_or_name for e in expressions]
            if len(names) != len(set(names)):
                raise UnresolvedLineage("DUPLICATE_OUTPUT_NAME")
            if names != result.columns:
                raise UnresolvedLineage("RESULT_SCHEMA_MISMATCH")
            # Fail closed for structures with dependencies beyond the characterized adapter.
            if tree.find(
                exp.Window,
                exp.Intersect,
                exp.Except,
                exp.Unnest,
                exp.Lateral,
                exp.Rollup,
                exp.Cube,
                exp.GroupingSets,
            ):
                raise UnresolvedLineage("UNSUPPORTED_LINEAGE_STRUCTURE")
            if any(s.find(exp.Select) for s in expressions):
                raise UnresolvedLineage("UNSUPPORTED_LINEAGE_STRUCTURE")
            root = build_scope(tree)
            if root is None:
                raise UnresolvedLineage("UNSUPPORTED_LINEAGE_STRUCTURE")
            if any(w.args.get("recursive") for w in tree.find_all(exp.With)):
                raise UnresolvedLineage("UNSUPPORTED_LINEAGE_STRUCTURE")
            for scope in root.traverse():
                if scope.subquery_scopes:
                    raise UnresolvedLineage("UNSUPPORTED_LINEAGE_STRUCTURE")
                if not isinstance(scope.expression, exp.Selectable):
                    raise UnresolvedLineage("UNSUPPORTED_LINEAGE_STRUCTURE")
                scoped_names = [e.alias_or_name for e in scope.expression.selects]
                if len(scoped_names) != len(set(scoped_names)):
                    raise UnresolvedLineage("DUPLICATE_OUTPUT_NAME")
            resolver = Resolver(root, self.schema)
            tables = sorted(resolver.rowset(root))
            nodes = lineage(
                None,
                tree,
                schema=self.schema.sqlglot_schema(),
                scope=root,
                dialect="duckdb",
                trim_selects=False,
            )
            for ordinal, name in enumerate(names):
                try:
                    cols, sources, kind = resolver.node(nodes[name])
                    code = {"CONSTANT": "LINEAGE_CONSTANT", "ROWSET": "LINEAGE_ROWSET"}.get(
                        kind, "LINEAGE_RESOLVED"
                    )
                    output = OutputColumnProvenance(
                        output_name=name,
                        output_ordinal=ordinal,
                        expression_sql=expressions[ordinal].sql(dialect="duckdb"),
                        source_columns=cols,
                        source_tables=sources,
                        dependency_kind=kind,
                        lineage_status="RESOLVED",
                        reason_code=code,
                    )
                except UnresolvedLineage as exc:
                    output = self.unresolved(name, ordinal, expressions, str(exc))
                outputs.append(output)
            context = build_context(resolver)
            if set(tables) != {f"main.{t}" for t in decision.analysis.physical_tables}:
                reasons.append("PHYSICAL_TABLE_MISMATCH")
        except (SqlglotError, UnresolvedLineage) as exc:
            reason = str(exc) if isinstance(exc, UnresolvedLineage) else "LINEAGE_SQLGLOT_ERROR"
            reasons.append(reason)
            outputs = [
                self.unresolved(n, i, expressions, reason) for i, n in enumerate(result.columns)
            ]
        reasons.extend(o.reason_code for o in outputs if o.lineage_status == "UNRESOLVED")
        reasons.extend(c.reason_code for c in context if c.status == "UNRESOLVED")
        status = "RESOLVED"
        if reasons:
            status = (
                "PARTIAL" if any(o.lineage_status == "RESOLVED" for o in outputs) else "UNRESOLVED"
            )
        record = QueryProvenance(
            query_fingerprint=decision.query_fingerprint,
            effective_sql=sql,
            source_tables=tables,
            output_columns=outputs,
            context=context,
            lineage_status=status,
            reason_codes=sorted(set(reasons)),
            provenance_fingerprint="",
        )
        return record.model_copy(update={"provenance_fingerprint": provenance_fingerprint(record)})

    @staticmethod
    def unresolved(
        name: str, ordinal: int, expressions: list[exp.Expr], reason: str
    ) -> OutputColumnProvenance:
        return OutputColumnProvenance(
            output_name=name,
            output_ordinal=ordinal,
            expression_sql=expressions[ordinal].sql(dialect="duckdb")
            if ordinal < len(expressions)
            else "",
            source_columns=[],
            source_tables=[],
            dependency_kind="MIXED",
            lineage_status="UNRESOLVED",
            reason_code=reason,
        )


def provenance_fingerprint(record: QueryProvenance) -> str:
    """Public canonical identity, shared with derivative validators."""
    return digest(record.model_dump(mode="json", by_alias=True, exclude={"provenance_fingerprint"}))

"""Resolve each predicate/grouping in its own scope, including nested CTE contexts."""

from typing import Literal

from sqlglot import exp
from sqlglot.errors import SqlglotError

from opslens.provenance.lineage import Resolver, UnresolvedLineage
from opslens.provenance.models import PhysicalColumnRef, QueryContextExpression


def build_context(resolver: Resolver) -> list[QueryContextExpression]:
    records = []
    for scope in resolver.scopes:
        tree = scope.expression
        if not isinstance(tree, exp.Select):
            continue
        expressions: list[tuple[Literal["WHERE", "HAVING", "JOIN_ON", "GROUP_BY"], exp.Expr]] = []
        clauses: list[tuple[str, Literal["WHERE", "HAVING"]]] = [
            ("where", "WHERE"),
            ("having", "HAVING"),
        ]
        for key, kind in clauses:
            clause = tree.args.get(key)
            if clause is not None:
                expressions.append((kind, clause.this))
        group = tree.args.get("group")
        if group:
            expressions.extend(("GROUP_BY", e) for e in group.expressions)
        for join in tree.args.get("joins") or []:
            if join.args.get("on") is not None:
                expressions.append(("JOIN_ON", join.args["on"]))
        for kind, expression in expressions:
            columns: dict[str, PhysicalColumnRef] = {}
            reason = "LINEAGE_RESOLVED"
            try:
                if expression.find(exp.Select, exp.Subquery):
                    raise UnresolvedLineage("UNSUPPORTED_LINEAGE_STRUCTURE")
                for column in expression.find_all(exp.Column):
                    for ref in resolver.column(scope, column):
                        columns[ref.canonical] = ref
            except (UnresolvedLineage, SqlglotError) as exc:
                reason = str(exc) if isinstance(exc, UnresolvedLineage) else "LINEAGE_SQLGLOT_ERROR"
                columns = {}
            records.append(
                QueryContextExpression(
                    kind=kind,
                    expression_sql=expression.sql(dialect="duckdb"),
                    source_columns=[columns[k] for k in sorted(columns)],
                    status="RESOLVED" if reason == "LINEAGE_RESOLVED" else "UNRESOLVED",
                    reason_code=reason,
                )
            )
    return records

"""DuckDB AST analysis and scope-qualified policy references, not output lineage."""

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError, SqlglotError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, traverse_scope
from sqlglot.schema import MappingSchema
from sqlglot.tokenizer_core import TokenType

from opslens.sql.models import SQLAnalysis
from opslens.sql.policy import SQLPolicyConfig
from opslens.sql.schema import DatabaseSchemaSnapshot

# Deterministic precedence, independent of traversal order and error prose.
REASON_PRIORITY = (
    "MULTI_STATEMENT",
    "SQL_LENGTH_EXCEEDED",
    "EMPTY_SQL",
    "PARSE_ERROR",
    "STATEMENT_NOT_ALLOWED",
    "EXTERNAL_ACCESS_FORBIDDEN",
    "SCHEMA_NOT_ALLOWED",
    "TABLE_FUNCTION_NOT_ALLOWED",
    "TABLE_NOT_ALLOWED",
    "FUNCTION_NOT_ALLOWED",
    "STAR_PROJECTION_FORBIDDEN",
    "AMBIGUOUS_COLUMN_REFERENCE",
    "UNKNOWN_COLUMN",
    "UNRESOLVED_COLUMN_REFERENCE",
    "COLUMN_DENIED",
    "LIMIT_EXCEEDED",
    "NON_LITERAL_LIMIT",
    "INVALID_OFFSET",
    "JOIN_BUDGET_EXCEEDED",
    "CTE_BUDGET_EXCEEDED",
    "SUBQUERY_BUDGET_EXCEEDED",
    "TABLE_REFERENCE_BUDGET_EXCEEDED",
)
FORBIDDEN_NODES = frozenset(
    (
        "insert",
        "update",
        "delete",
        "merge",
        "create",
        "drop",
        "alter",
        "truncatetable",
        "copy",
        "attach",
        "detach",
        "install",
        "load",
        "import",
        "export",
        "pragma",
        "set",
        "command",
        "transaction",
        "commit",
        "rollback",
        "use",
        "grant",
        "revoke",
        "into",
        "lock",
        "execute",
        "declare",
        "kill",
        "pivot",
        "unpivot",
        "tablesample",
    )
)
QUERY_TYPES = (exp.Select, exp.Union, exp.Intersect, exp.Except)


def function_name(node: exp.Func) -> str:
    return (node.name if isinstance(node, exp.Anonymous) else node.sql_name()).lower()


def integer_literal(node: exp.Expr | None) -> int | None:
    if isinstance(node, exp.Literal) and not node.is_string and node.this.isdigit():
        return int(node.this)
    return None


def _source_columns(source: exp.Expr | Scope, schema: dict) -> set[str]:
    if isinstance(source, exp.Table):
        return set(schema.get("main", {}).get(source.name, {}))
    if isinstance(source, Scope) and isinstance(source.expression, exp.Query):
        return set(source.expression.named_selects)
    return set()


def resolve_columns(
    tree: exp.Expr, snapshot: DatabaseSchemaSnapshot
) -> tuple[exp.Expr | None, list[str], list[str]]:
    """Use SQLGlot qualification, inspect unresolved scopes without parsing exception text."""
    schema = snapshot.sqlglot_schema()
    try:
        qualified = qualify(
            tree.copy(),
            dialect="duckdb",
            db="main",
            schema=MappingSchema(dict(schema), dialect="duckdb"),
            infer_schema=False,
            validate_qualify_columns=False,
        )
    except OptimizeError:
        # The partial pass below separates unknown physical columns from uncertain constructs.
        for scope in traverse_scope(tree):
            for col in scope.columns:
                source = scope.sources.get(col.table)
                if isinstance(source, (exp.Table, Scope)) and col.name not in _source_columns(
                    source, schema
                ):
                    return None, [], ["UNKNOWN_COLUMN"]
        return None, [], ["UNRESOLVED_COLUMN_REFERENCE"]
    refs, reasons = set(), set()
    for scope in traverse_scope(qualified):
        for col in scope.columns:
            if not col.table:
                matches = sum(
                    col.name in _source_columns(s, schema)
                    for _, s in scope.selected_sources.values()
                )
                reasons.add("AMBIGUOUS_COLUMN_REFERENCE" if matches > 1 else "UNKNOWN_COLUMN")
                continue
            owner = scope
            source = owner.sources.get(col.table)
            while source is None and owner.parent is not None and scope.can_be_correlated:
                owner = owner.parent
                source = owner.sources.get(col.table)
            if source is None:
                reasons.add("UNRESOLVED_COLUMN_REFERENCE")
            elif col.name not in _source_columns(source, schema):
                reasons.add("UNKNOWN_COLUMN")
            elif isinstance(source, exp.Table):
                refs.add(f"{source.name}.{col.name}")
    if not reasons:
        try:
            qualify(
                qualified.copy(),
                dialect="duckdb",
                db="main",
                schema=MappingSchema(dict(schema), dialect="duckdb"),
                infer_schema=False,
            )
        except OptimizeError:
            reasons.add("UNRESOLVED_COLUMN_REFERENCE")
    return qualified, sorted(refs), sorted(reasons)


def analyze(
    sql: str, policy: SQLPolicyConfig, snapshot: DatabaseSchemaSnapshot
) -> tuple[SQLAnalysis, exp.Expr | None, list[str]]:
    data: dict = {"original_sql": sql}
    reasons: set[str] = set()
    # Bound parser work before attempting to parse untrusted input.
    if len(sql) > policy.max_sql_length:
        return SQLAnalysis(**data), None, ["SQL_LENGTH_EXCEEDED"]
    try:
        statements = [
            s
            for s in sqlglot.parse(sql, read="duckdb")
            if s is not None and not isinstance(s, exp.Semicolon)
        ]
    except (ParseError, RecursionError, SqlglotError) as exc:
        data["parse_error"] = type(exc).__name__
        # Preserve multi-statement priority even when a later statement is malformed.
        # SQLGlot tokens distinguish literal/comment semicolons from SQL separators.
        try:
            count, has_tokens = 0, False
            for token in sqlglot.tokenize(sql, read="duckdb"):
                if token.token_type == TokenType.SEMICOLON:
                    count += has_tokens
                    has_tokens = False
                else:
                    has_tokens = True
            count += has_tokens
            if count > 1:
                data["statement_count"] = count
                return SQLAnalysis(**data), None, ["MULTI_STATEMENT"]
        except SqlglotError:
            pass
        return SQLAnalysis(**data), None, ["PARSE_ERROR"]
    data["statement_count"] = len(statements)
    if len(statements) != 1:
        return SQLAnalysis(**data), None, ["MULTI_STATEMENT" if statements else "EMPTY_SQL"]
    tree = statements[0]
    # DuckDB identifiers are case insensitive, including quoted identifiers.
    for ident in tree.find_all(exp.Identifier):
        ident.set("this", ident.this.lower())
    data.update(
        normalized_sql=tree.sql(dialect="duckdb", comments=False), statement_type=tree.key.upper()
    )
    if (
        not isinstance(tree, QUERY_TYPES)
        or any(n.key in FORBIDDEN_NODES for n in tree.walk())
        or any(w.args.get("recursive") for w in tree.find_all(exp.With))
    ):
        reasons.add("STATEMENT_NOT_ALLOWED")
    # Every nested query must also be an explicitly supported read-only query.
    if any(not isinstance(s.this, QUERY_TYPES) for s in tree.find_all(exp.CTE, exp.Subquery)):
        reasons.add("STATEMENT_NOT_ALLOWED")
    tables, schemas, table_functions = set(), set(), set()
    functions = {function_name(f) for f in tree.find_all(exp.Func)}
    data["functions"] = sorted(functions)
    data["join_count"] = len(list(tree.find_all(exp.Join)))
    data["cte_count"] = len(list(tree.find_all(exp.CTE)))
    data["subquery_count"] = len(list(tree.find_all(exp.Subquery)))
    # Count all table AST references, including CTE uses, to bound expansion.
    data["table_reference_count"] = len(list(tree.find_all(exp.Table)))
    stars = [
        s
        for s in tree.find_all(exp.Star)
        if not (isinstance(s.parent, exp.Count) and s.parent.this is s)
    ]
    column_selectors = list(tree.find_all(exp.Columns))
    data["has_projection_star"] = bool(stars or column_selectors)
    if column_selectors or (stars and not policy.allow_star_projection):
        reasons.add("STAR_PROJECTION_FORBIDDEN")
    try:
        for scope in traverse_scope(tree):
            for _, source in scope.selected_sources.values():
                if isinstance(source, Scope):
                    continue
                if not isinstance(source, exp.Table) or not isinstance(source.this, exp.Identifier):
                    table_functions.add(source.sql(dialect="duckdb").split("(")[0].lower())
                    reasons.add("TABLE_FUNCTION_NOT_ALLOWED")
                    continue
                tables.add(source.name)
                schemas.add(source.db or "main")
                if source.catalog or (source.db and source.db not in policy.allowed_schemas):
                    reasons.add("SCHEMA_NOT_ALLOWED")
                if source.name not in policy.allowed_tables:
                    reasons.add("TABLE_NOT_ALLOWED")
                if source.args.get("alias") and source.args["alias"].args.get("columns"):
                    reasons.add("UNRESOLVED_COLUMN_REFERENCE")
        # Lateral/UNNEST/VALUES and non-table sources can be represented as scopes.
        for node in tree.find_all(exp.From, exp.Join):
            if not isinstance(node.this, (exp.Table, exp.Subquery)):
                reasons.add("TABLE_FUNCTION_NOT_ALLOWED")
                table_functions.add(node.this.key)
    except (OptimizeError, SqlglotError):
        reasons.add("UNRESOLVED_COLUMN_REFERENCE")
    data.update(
        physical_tables=sorted(tables),
        schemas=sorted(schemas),
        table_functions=sorted(table_functions),
    )
    if functions.intersection(policy.forbidden_functions):
        reasons.add("FUNCTION_NOT_ALLOWED")
    for counter, maximum, reason in (
        ("join_count", policy.max_joins, "JOIN_BUDGET_EXCEEDED"),
        ("cte_count", policy.max_ctes, "CTE_BUDGET_EXCEEDED"),
        ("subquery_count", policy.max_subqueries, "SUBQUERY_BUDGET_EXCEEDED"),
        ("table_reference_count", policy.max_table_references, "TABLE_REFERENCE_BUDGET_EXCEEDED"),
    ):
        if data[counter] > maximum:
            reasons.add(reason)
    limit = tree.args.get("limit")
    if limit is not None:
        value = integer_literal(limit.expression)
        data["top_level_limit"] = value
        if value is None or limit.args.get("limit_options"):
            reasons.add("NON_LITERAL_LIMIT")
        elif value > policy.max_result_rows:
            reasons.add("LIMIT_EXCEEDED")
    for offset in tree.find_all(exp.Offset):
        if integer_literal(offset.expression) is None:
            reasons.add("INVALID_OFFSET")
    # Inner limits must also be static: parameters otherwise survive into execution.
    for inner in tree.find_all(exp.Limit):
        if integer_literal(inner.expression) is None or inner.args.get("limit_options"):
            reasons.add("NON_LITERAL_LIMIT")
    if not reasons and tree.find(exp.Placeholder, exp.Parameter):
        reasons.add("UNRESOLVED_COLUMN_REFERENCE")
    if not reasons:
        qualified, refs, column_reasons = resolve_columns(tree, snapshot)
        data["columns"] = refs
        reasons.update(column_reasons)
        if set(refs).intersection(policy.denied_columns):
            reasons.add("COLUMN_DENIED")
        if qualified is not None:
            tree = qualified
    return SQLAnalysis(**data), tree, [r for r in REASON_PRIORITY if r in reasons]

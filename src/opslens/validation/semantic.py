"""Conservative SQLGlot proofs for a deliberately bounded relational fragment."""

from datetime import datetime

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, build_scope

from opslens.metrics import MetricRegistry
from opslens.metrics.models import MetricDefinition, MetricFilter
from opslens.sql.schema import DatabaseSchemaSnapshot
from opslens.validation.intent import AnalysisIntent
from opslens.validation.models import ValidationCheck
from opslens.validation.oracle import REFUND_DSL, UnsupportedContract, formula
from opslens.validation.status import check


def canonical(node: exp.Expr) -> str:
    def clean(n: exp.Expr) -> exp.Expr:
        if isinstance(n, (exp.Paren, exp.Alias)):
            return n.this
        if isinstance(n, exp.Identifier):
            return exp.to_identifier(n.name.lower())
        return n

    return node.copy().transform(clean).sql(dialect="duckdb", normalize=True)


def contract_expression(d: MetricDefinition, registry: MetricRegistry) -> exp.Expr:
    if d.expression == REFUND_DSL:
        numerator = next(f for f in d.filters if f.scope == "NUMERATOR")
        if numerator.operator != "EQ" or numerator.column != "refunds.refund_status":
            raise UnsupportedContract("NUMERATOR_DSL_UNSUPPORTED")
        predicate = exp.EQ(
            this=exp.column("refund_status", table="refunds"),
            expression=exp.convert(numerator.value),
        )
        numerator_node = exp.Count(
            this=exp.Distinct(
                expressions=[
                    exp.Case(
                        ifs=[exp.If(this=predicate, true=exp.column("order_id", table="orders"))]
                    )
                ]
            )
        )
        dep = registry.active("paid_order_count")
        assert dep is not None
        return exp.Div(
            this=numerator_node,
            expression=exp.Nullif(
                this=contract_expression(dep, registry), expression=exp.Literal.number(0)
            ),
        )
    tree = formula(d)
    if d.metric_type == "BASE":
        return tree

    def replace(node: exp.Expr) -> exp.Expr:
        if isinstance(node, exp.Column) and node.name in d.dependencies:
            dep = registry.active(node.name)
            assert dep is not None
            return contract_expression(dep, registry)
        return node

    tree = tree.transform(replace)
    # Contract division explicitly specifies null on zero.
    if isinstance(tree, exp.Div):
        tree.set("expression", exp.Nullif(this=tree.expression, expression=exp.Literal.number(0)))
    return tree


def predicate(item: MetricFilter) -> exp.Expr:
    column = sqlglot.parse_one(item.column)
    if item.operator in ("IN", "NOT_IN"):
        assert isinstance(item.value, list)
        node = exp.In(this=column, expressions=[exp.convert(v) for v in item.value])
        return exp.Not(this=node) if item.operator == "NOT_IN" else node
    if item.operator in ("IS_NULL", "IS_NOT_NULL"):
        node = exp.Is(this=column, expression=exp.Null())
        return exp.Not(this=node) if item.operator == "IS_NOT_NULL" else node
    return (exp.EQ if item.operator == "EQ" else exp.NEQ)(
        this=column, expression=exp.convert(item.value)
    )


def conjunctions(node: exp.Expr) -> list[exp.Expr]:
    if isinstance(node, (exp.Where, exp.Paren)):
        return conjunctions(node.this)
    if isinstance(node, exp.And):
        return conjunctions(node.this) + conjunctions(node.expression)
    return [node]


class Candidate:
    def __init__(self, sql: str, schema: DatabaseSchemaSnapshot):
        tree = qualify(
            sqlglot.parse_one(sql, read="duckdb"),
            dialect="duckdb",
            schema=dict(schema.sqlglot_schema()),
            validate_qualify_columns=True,
        )
        root = build_scope(tree)
        if root is None or not isinstance(root.expression, exp.Select):
            raise UnsupportedContract("QUERY_SCOPE_UNSUPPORTED")
        self.root = root
        self.scopes: list[Scope] = []

    def expand(self, node: exp.Expr, scope: Scope, depth: int = 0) -> exp.Expr:
        if depth > 40:
            raise UnsupportedContract("EXPRESSION_DEPTH_UNSUPPORTED")
        if scope not in self.scopes:
            self.scopes.append(scope)
        if isinstance(node, exp.Alias):
            return self.expand(node.this, scope, depth + 1)
        if isinstance(node, exp.Column):
            source = scope.sources.get(node.table)
            if isinstance(source, exp.Table):
                return exp.column(node.name, table=source.name)
            if isinstance(source, Scope):
                if not isinstance(source.expression, exp.Select):
                    raise UnsupportedContract("NON_SELECT_SCOPE")
                matches = [s for s in source.expression.selects if s.alias_or_name == node.name]
                if len(matches) != 1:
                    raise UnsupportedContract("AMBIGUOUS_DERIVED_COLUMN")
                return self.expand(matches[0], source, depth + 1)
            raise UnsupportedContract("COLUMN_SCOPE_UNSUPPORTED")
        copied = node.copy()
        for key, value in list(node.args.items()):
            if isinstance(value, exp.Expr):
                copied.set(key, self.expand(value, scope, depth + 1))
            elif isinstance(value, list):
                copied.set(
                    key,
                    [
                        self.expand(v, scope, depth + 1) if isinstance(v, exp.Expr) else v
                        for v in value
                    ],
                )
        return copied


def validate_semantics(
    sql: str,
    intent: AnalysisIntent,
    d: MetricDefinition,
    registry: MetricRegistry,
    schema: DatabaseSchemaSnapshot,
    source_columns: set[str],
) -> list[ValidationCheck]:
    checks: list[ValidationCheck] = []
    candidate = Candidate(sql, schema)
    assert isinstance(candidate.root.expression, exp.Select)
    outputs = {s.alias_or_name: s for s in candidate.root.expression.selects}
    if intent.metric_output_column not in outputs:
        return [check("METRIC_EXPRESSION_DEPENDENCY_MISMATCH", "FAIL")]
    actual = candidate.expand(outputs[intent.metric_output_column], candidate.root)
    expected = contract_expression(d, registry)
    required_columns = {f"{c.table}.{c.name}" for c in expected.find_all(exp.Column)}
    if not required_columns <= source_columns:
        checks.append(check("METRIC_EXPRESSION_DEPENDENCY_MISMATCH", "FAIL"))
    checks.append(
        check(
            "METRIC_FORMULA_VALID"
            if canonical(actual) == canonical(expected)
            else "METRIC_FORMULA_MISMATCH",
            "PASS" if canonical(actual) == canonical(expected) else "FAIL",
        )
    )
    # Predicates are expanded per scope. Unused CTEs cannot satisfy a required predicate.
    scopes = list(candidate.scopes)
    predicates: list[exp.Expr] = []
    for scope in scopes:
        where = scope.expression.args.get("where")
        if where is not None:
            predicates.extend(candidate.expand(p, scope) for p in conjunctions(where))
    all_filters = list(d.filters)

    def dependencies(definition: MetricDefinition):
        for dep in definition.dependencies:
            target = registry.active(dep)
            assert target is not None
            all_filters.extend(target.filters)
            dependencies(target)

    dependencies(d)
    # A predicate in one component cannot validate another component's aggregate.
    definitions = [d]

    def collect(definition: MetricDefinition):
        for dep in definition.dependencies:
            target = registry.active(dep)
            assert target is not None
            definitions.append(target)
            collect(target)

    collect(d)
    for scope in scopes:
        if not isinstance(scope.expression, exp.Select):
            raise UnsupportedContract("NON_SELECT_SCOPE")
        native_aggregates = [
            a for projection in scope.expression.selects for a in projection.find_all(exp.AggFunc)
        ]
        where = scope.expression.args.get("where")
        local_predicates = (
            [candidate.expand(p, scope) for p in conjunctions(where)] if where else []
        )
        local_signatures = {canonical(p) for p in local_predicates}
        for aggregate_node in native_aggregates:
            signature = canonical(candidate.expand(aggregate_node, scope))
            for definition in definitions:
                if definition.metric_type != "BASE" or signature != canonical(formula(definition)):
                    continue
                for item in definition.filters + list(intent.filters):
                    if item.scope == "ALL" and canonical(predicate(item)) not in local_signatures:
                        checks.append(check("REQUIRED_METRIC_FILTER_MISSING", "FAIL"))
                if intent.time_window:
                    time_column = definition.time_semantics.time_column
                    local_time = [
                        p
                        for p in local_predicates
                        if isinstance(p, (exp.GTE, exp.LT))
                        and isinstance(p.this, exp.Column)
                        and f"{p.this.table}.{p.this.name}" == time_column
                    ]
                    if len(local_time) != 2:
                        checks.append(check("TIME_WINDOW_EQUIVALENCE_UNRESOLVED", "UNKNOWN"))
    for item in all_filters + list(intent.filters):
        if item.scope == "NUMERATOR":
            # Exact CASE numerator / unfiltered distinct denominator is the supported adapter.
            if canonical(actual) != canonical(expected):
                checks.append(check("NUMERATOR_FILTER_SCOPE_UNRESOLVED", "UNKNOWN"))
            continue
        target = canonical(predicate(item))
        if target not in {canonical(p) for p in predicates}:
            column_present = any(
                item.column in {f"{c.table}.{c.name}" for c in p.find_all(exp.Column)}
                for p in predicates
            )
            checks.append(
                check(
                    "REQUIRED_METRIC_FILTER_MISMATCH"
                    if column_present
                    else "REQUIRED_METRIC_FILTER_MISSING",
                    "FAIL",
                )
            )
    if intent.time_window:
        if intent.time_window.timezone != d.time_semantics.timezone:
            checks.append(check("TIME_WINDOW_EQUIVALENCE_UNRESOLVED", "UNKNOWN"))
        start, end = intent.time_window.local_bounds()
        time_column = d.time_semantics.time_column
        bounds: dict[str, datetime] = {}
        wrong_time = False
        for p in predicates:
            if isinstance(p, (exp.GTE, exp.LT)) and isinstance(p.this, exp.Column):
                name = f"{p.this.table}.{p.this.name}"
                if name.endswith(("_at", "_date")) and name != time_column:
                    wrong_time = True
                if name == time_column:
                    value = p.expression
                    if isinstance(value, exp.Cast):
                        value = value.this
                    if isinstance(value, exp.Literal) and value.is_string:
                        try:
                            bounds["start" if isinstance(p, exp.GTE) else "end"] = (
                                datetime.fromisoformat(value.this)
                            )
                        except ValueError:
                            pass
        if wrong_time:
            checks.append(check("METRIC_TIME_COLUMN_MISMATCH", "FAIL"))
        elif not bounds:
            checks.append(check("TIME_WINDOW_EQUIVALENCE_UNRESOLVED", "UNKNOWN"))
        elif bounds != {"start": start, "end": end}:
            checks.append(check("TIME_WINDOW_MISMATCH", "FAIL"))
        else:
            checks.append(check("TIME_WINDOW_VALID"))
    if not set(intent.dimensions) <= set(d.allowed_dimensions):
        checks.append(check("DIMENSION_NOT_ALLOWED", "FAIL"))
    names = {c.split(".")[1] for c in intent.dimensions}
    for dimension in intent.dimensions:
        name = dimension.split(".")[1]
        if (
            name not in outputs
            or canonical(candidate.expand(outputs[name], candidate.root)) != dimension
        ):
            checks.append(check("REQUESTED_DIMENSION_MISSING", "FAIL"))
    if intent.time_grain:
        names.add("time_bucket")
        if intent.time_grain not in d.time_semantics.allowed_grains:
            checks.append(check("TIME_GRAIN_NOT_ALLOWED", "FAIL"))
        target = sqlglot.parse_one(
            f"DATE_TRUNC('{intent.time_grain}', {d.time_semantics.time_column})", read="duckdb"
        )
        if "time_bucket" not in outputs or canonical(
            candidate.expand(outputs["time_bucket"], candidate.root)
        ) != canonical(target):
            checks.append(check("TIME_GRAIN_UNRESOLVED", "UNKNOWN"))
    if set(outputs) - {intent.metric_output_column} != names:
        checks.append(check("UNEXPECTED_DIMENSION", "FAIL"))
    # Grouping must implement exactly the requested grain; hidden extra grouping fails too.
    for scope in scopes:
        group = scope.expression.args.get("group")
        if group:
            actual_group = {canonical(candidate.expand(g, scope)) for g in group.expressions}
            expected_group = set(intent.dimensions)
            if intent.time_grain:
                expected_group.add(
                    canonical(
                        sqlglot.parse_one(
                            f"DATE_TRUNC('{intent.time_grain}', {d.time_semantics.time_column})",
                            read="duckdb",
                        )
                    )
                )
            if actual_group != expected_group:
                checks.append(check("UNEXPECTED_DIMENSION", "FAIL"))
    return checks or [check("METRIC_SEMANTICS_VALID")]

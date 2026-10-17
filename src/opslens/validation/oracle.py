"""Independent contract evaluator on the trusted demo schema.

Only registry, intent and read-only database are inputs. This is not a user SQL generator.
"""

from decimal import Decimal
from pathlib import Path
from typing import Protocol

import sqlglot
from pydantic import Field, JsonValue
from sqlglot import exp

from opslens.metrics import MetricRegistry
from opslens.metrics.models import MetricDefinition, MetricFilter
from opslens.sql.executor import serialize_cell
from opslens.sql.models import StrictModel
from opslens.sql.schema import readonly_connection
from opslens.validation.intent import AnalysisIntent

REFUND_DSL = (
    "COUNT_DISTINCT(orders.order_id WHERE EXISTS approved refunds FOR orders.order_id)"
    " / paid_order_count"
)


class UnsupportedContract(ValueError):
    pass


class OracleResult(StrictModel):
    oracle_id: str = "duckdb_metric_oracle"
    oracle_version: str = "m6.0"
    columns: list[str]
    rows: list[list[JsonValue]] = Field(default_factory=list)


class MetricResultOracle(Protocol):
    oracle_id: str
    oracle_version: str

    def evaluate(self, intent: AnalysisIntent) -> OracleResult: ...


def formula(definition: MetricDefinition) -> exp.Expr:
    expression = definition.expression
    if expression == REFUND_DSL:
        expression = "refunded_orders / paid_order_count"
    if expression.startswith("COUNT_DISTINCT("):
        expression = "COUNT(DISTINCT " + expression[len("COUNT_DISTINCT(") :]
    tree = sqlglot.parse_one(expression, read="duckdb")
    allowed = (
        exp.Sum,
        exp.Count,
        exp.Distinct,
        exp.Column,
        exp.Identifier,
        exp.Mul,
        exp.Sub,
        exp.Add,
        exp.Div,
        exp.Paren,
    )
    if any(not isinstance(n, allowed) for n in tree.walk()):
        raise UnsupportedContract("UNSUPPORTED_CONTRACT_EXPRESSION")
    return tree


def filter_sql(item: MetricFilter, params: list) -> str:
    column = item.column
    if item.operator in ("IS_NULL", "IS_NOT_NULL"):
        return f"{column} IS {'NOT ' if item.operator == 'IS_NOT_NULL' else ''}NULL"
    if item.operator in ("IN", "NOT_IN"):
        assert isinstance(item.value, list)
        params.extend(item.value)
        return (
            f"{column} {'NOT IN' if item.operator == 'NOT_IN' else 'IN'} ("
            + ",".join("?" for _ in item.value)
            + ")"
        )
    params.append(item.value)
    return f"{column} {'=' if item.operator == 'EQ' else '<>'} ?"


class DuckDBMetricOracle:
    oracle_id = "duckdb_metric_oracle"
    oracle_version = "m6.0"

    def __init__(self, db: Path, registry: MetricRegistry):
        self.db = db
        self.registry = registry

    def evaluate(self, intent: AnalysisIntent) -> OracleResult:
        intent = AnalysisIntent.model_validate(intent.model_dump())
        definition = self.registry.get(intent.metric_id, intent.metric_version)
        if definition is None:
            raise UnsupportedContract("UNKNOWN_METRIC_VERSION")
        if not set(intent.dimensions) <= set(definition.allowed_dimensions):
            raise UnsupportedContract("DIMENSION_NOT_ALLOWED")
        if intent.time_grain and intent.time_grain not in definition.time_semantics.allowed_grains:
            raise UnsupportedContract("TIME_GRAIN_NOT_ALLOWED")
        if intent.time_window and intent.time_window.timezone != definition.time_semantics.timezone:
            raise UnsupportedContract("TIMEZONE_UNSUPPORTED")
        # Arbitrary identifiers never enter SQL. All columns come from the validated registry.
        if any(f.column not in definition.allowed_dimensions for f in intent.filters):
            raise UnsupportedContract("INTENT_FILTER_UNSUPPORTED")
        keys = [d.split(".")[1] for d in intent.dimensions]
        if intent.time_grain:
            keys.append("time_bucket")
        with readonly_connection(self.db) as connection:
            connection.execute("BEGIN TRANSACTION")

            def base(d: MetricDefinition, refunded: bool = False) -> dict[tuple, object]:
                params: list = []
                dimensions = list(intent.dimensions)
                if intent.time_grain:
                    dimensions.append(
                        f"DATE_TRUNC('{intent.time_grain}', {d.time_semantics.time_column})"
                    )
                predicates = [filter_sql(f, params) for f in d.filters if f.scope == "ALL"]
                predicates.extend(filter_sql(f, params) for f in intent.filters)
                if intent.time_window:
                    start, end = intent.time_window.local_bounds()
                    predicates += [
                        f"{d.time_semantics.time_column} >= ?",
                        f"{d.time_semantics.time_column} < ?",
                    ]
                    params.extend([start, end])
                expression = (
                    formula(d).sql(dialect="duckdb")
                    if not refunded
                    else ("COUNT(DISTINCT orders.order_id)")
                )
                needed = set(d.source_tables) | {c.split(".")[0] for c in intent.dimensions}
                needed |= {f.column.split(".")[0] for f in intent.filters}
                if refunded:
                    numerator = [f for f in d.filters if f.scope == "NUMERATOR"]
                    if len(numerator) != 1 or d.expression != REFUND_DSL:
                        raise UnsupportedContract("NUMERATOR_DSL_UNSUPPORTED")
                    predicates.append(
                        "EXISTS (SELECT 1 FROM refunds WHERE "
                        "refunds.order_id = orders.order_id AND "
                        + filter_sql(numerator[0], params)
                        + ")"
                    )
                    needed.discard("refunds")
                source = "orders"
                if "products" in needed:
                    needed.add("order_items")
                for table, join in (
                    ("order_items", "order_items.order_id = orders.order_id"),
                    ("refunds", "refunds.order_id = orders.order_id"),
                    ("channels", "channels.channel_id = orders.channel_id"),
                    ("products", "products.product_id = order_items.product_id"),
                ):
                    if table in needed:
                        source += f" JOIN {table} ON {join}"
                sql = "SELECT " + ", ".join(dimensions + [expression]) + " FROM " + source
                if predicates:
                    sql += " WHERE " + " AND ".join(predicates)
                if dimensions:
                    sql += " GROUP BY " + ", ".join(str(i + 1) for i in range(len(dimensions)))
                return {tuple(r[:-1]): r[-1] for r in connection.execute(sql, params).fetchall()}

            def evaluate(d: MetricDefinition) -> dict[tuple, object]:
                if d.metric_type == "BASE":
                    return base(d)
                operands = {}
                for dep in d.dependencies:
                    target = self.registry.active(dep)
                    if target is None:
                        raise UnsupportedContract("MISSING_DEPENDENCY")
                    operands[dep] = evaluate(target)
                if d.expression == REFUND_DSL:
                    operands["refunded_orders"] = base(d, refunded=True)
                tree = formula(d)
                all_keys = set().union(*(set(v) for v in operands.values()))

                def value(node: exp.Expr, key: tuple):
                    if isinstance(node, exp.Paren):
                        return value(node.this, key)
                    if isinstance(node, exp.Column) and node.name in operands:
                        return operands[node.name].get(key, 0)
                    if isinstance(node, (exp.Add, exp.Sub, exp.Mul, exp.Div)):
                        left, right = value(node.this, key), value(node.expression, key)
                        if left is None or right is None:
                            return None
                        left, right = Decimal(str(left)), Decimal(str(right))
                        if isinstance(node, exp.Div):
                            return float(left) / float(right) if right else None
                        if isinstance(node, exp.Sub):
                            return left - right
                        if isinstance(node, exp.Add):
                            return left + right
                        return left * right
                    raise UnsupportedContract("UNSUPPORTED_DERIVED_EXPRESSION")

                return {key: value(tree, key) for key in all_keys}

            values = evaluate(definition)
            connection.execute("COMMIT")
        return OracleResult(
            columns=keys + [intent.metric_output_column],
            rows=[
                [serialize_cell(v) for v in (*key, val)]
                for key, val in sorted(values.items(), key=lambda pair: repr(pair[0]))
            ],
        )

"""Observed public SQLGlot 30.18 behavior, before the OpsLens adapter."""

import pytest
from sqlglot import exp
from sqlglot.lineage import lineage

SCHEMA = {
    "main": {
        "orders": {"order_id": "VARCHAR", "region": "VARCHAR"},
        "order_items": {"order_id": "VARCHAR", "quantity": "INTEGER", "unit_price": "DECIMAL"},
    }
}


@pytest.mark.parametrize(
    "sql,leaves",
    [
        ("SELECT region FROM main.orders", {"orders.region"}),
        (
            "SELECT quantity * unit_price AS x FROM main.order_items",
            {"order_items.quantity", "order_items.unit_price"},
        ),
        ("SELECT SUM(quantity) AS x FROM main.order_items", {"order_items.quantity"}),
        ("SELECT COUNT(*) AS x FROM main.orders", set()),
        (
            "SELECT o.region FROM main.orders o JOIN main.order_items i ON o.order_id=i.order_id",
            {"o.region"},
        ),
        ("WITH a AS (SELECT region FROM main.orders) SELECT region FROM a", {"orders.region"}),
        ("SELECT a.region FROM (SELECT region FROM main.orders) a", {"orders.region"}),
        (
            "SELECT region FROM main.orders UNION ALL SELECT order_id FROM main.orders",
            {"orders.region", "orders.order_id"},
        ),
        (
            "SELECT CASE WHEN quantity>1 THEN unit_price ELSE 0 END AS x FROM main.order_items",
            {"order_items.quantity", "order_items.unit_price"},
        ),
    ],
)
def test_public_lineage(sql, leaves):
    nodes = lineage(None, sql, schema=SCHEMA, dialect="duckdb")
    assert len(nodes) == 1
    root = next(iter(nodes.values()))
    assert root.expression.sql() and root.source.sql()
    assert {n.name for n in root.walk() if isinstance(n.expression, exp.Table)} == leaves
    if "COUNT(*)" in sql:
        assert not root.downstream  # Adapter must supply explicit rowset identity.

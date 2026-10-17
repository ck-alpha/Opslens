import duckdb
import pytest

from opslens.demo.config import TABLES, DemoConfig
from opslens.demo.generator import fingerprints, generate_demo_data, validate_integrity


def test_reproducibility_and_seed(tmp_path):
    reports = [
        generate_demo_data(
            tmp_path / f"{i}.duckdb", DemoConfig(seed=seed, orders_per_channel_month=8)
        )
        for i, seed in enumerate((123, 123, 456))
    ]
    assert reports[0]["row_counts"] == reports[1]["row_counts"]
    assert reports[0]["fingerprints"] == reports[1]["fingerprints"]
    assert reports[0]["fingerprints"]["orders"] != reports[2]["fingerprints"]["orders"]
    schemas = []
    for i in range(3):
        with duckdb.connect(str(tmp_path / f"{i}.duckdb"), read_only=True) as conn:
            schemas.append(
                conn.execute(
                    "SELECT table_name, column_name, data_type "
                    "FROM information_schema.columns "
                    "ORDER BY table_name, ordinal_position"
                ).fetchall()
            )
    assert schemas[0] == schemas[1] == schemas[2]
    with pytest.raises(FileExistsError):
        generate_demo_data(tmp_path / "0.duckdb")


def test_integrity_and_event_bounds(demo_db):
    with duckdb.connect(str(demo_db), read_only=True) as conn:
        validate_integrity(conn)
        for child, key, parent in [
            ("orders", "channel_id", "channels"),
            ("order_items", "order_id", "orders"),
            ("order_items", "product_id", "products"),
            ("refunds", "order_id", "orders"),
            ("refunds", "order_item_id", "order_items"),
        ]:
            assert (
                conn.execute(
                    f"SELECT count(*) FROM {child} c LEFT JOIN {parent} p "
                    f"ON c.{key}=p.{key} WHERE c.{key} IS NOT NULL "
                    f"AND p.{key} IS NULL"
                ).fetchone()[0]
                == 0
            )
        assert (
            conn.execute(
                "SELECT count(*) FROM order_items WHERE quantity <= 0 OR "
                "unit_price < 0 OR discount_amount < 0 OR "
                "discount_amount > quantity * unit_price"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute("SELECT count(*) FROM refunds WHERE refund_amount < 0").fetchone()[0] == 0
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM orders WHERE "
                "(order_status IN ('paid','completed') AND paid_at IS NULL) OR "
                "(order_status='cancelled' AND cancelled_at IS NULL)"
            ).fetchone()[0]
            == 0
        )
        for table, columns in [
            ("orders", ("created_at", "paid_at", "cancelled_at")),
            ("refunds", ("created_at",)),
        ]:
            for column in columns:
                assert (
                    conn.execute(
                        f"SELECT count(*) FROM {table} WHERE "
                        f"{column} < TIMESTAMP '2026-06-01' OR "
                        f"{column} >= TIMESTAMP '2026-09-01'"
                    ).fetchone()[0]
                    == 0
                )
        assert set(fingerprints(conn)) == set(TABLES)


def test_scenario_mechanisms(tmp_path):
    # Extreme probabilities test the intervention wiring without sampling uncertainty.
    db = tmp_path / "scenario.duckdb"
    generate_demo_data(
        db,
        DemoConfig(
            orders_per_channel_month=40,
            base_cancel_probability=0,
            affiliate_aug_cancel_probability=1,
            base_refund_probability=0,
            electronics_aug_refund_probability=1,
        ),
    )
    with duckdb.connect(str(db), read_only=True) as conn:
        assert conn.execute(
            "SELECT month(created_at), count(*) FROM orders WHERE "
            "channel_id='social' GROUP BY 1 ORDER BY 1"
        ).fetchall() == [(6, 40), (7, 40), (8, 16)]
        assert (
            conn.execute(
                "SELECT count(*) FROM orders WHERE order_status='cancelled' "
                "AND (channel_id<>'affiliate' OR month(created_at)<>8)"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute("SELECT count(*) FROM orders WHERE order_status='cancelled'").fetchone()[0]
            == 40
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM refunds r JOIN order_items USING(order_item_id) "
                "JOIN products USING(product_id) WHERE category<>'electronics' "
                "OR month(r.created_at)<>8"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute("SELECT count(*) FROM refunds WHERE refund_status='approved'").fetchone()[
                0
            ]
            > 0
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"orders_per_channel_month": 0},
        {"social_aug_volume_ratio": 2},
        {"base_refund_probability": -1},
    ],
)
def test_invalid_config(kwargs):
    with pytest.raises(ValueError):
        DemoConfig(**kwargs)

"""Deterministic, offline generation with explicit controlled interventions."""

import calendar
import hashlib
import json
import random
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import duckdb

from opslens.demo.config import GENERATOR_VERSION, SCHEMA_VERSION, TABLES, DemoConfig
from opslens.demo.schema import SCHEMA_SQL


def fingerprints(connection: duckdb.DuckDBPyConnection) -> dict[str, str]:
    """Hash canonical logical rows, excluding wall-clock metadata and file layout."""
    return {
        table: hashlib.sha256(
            json.dumps(
                connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall(),
                default=str,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        for table in TABLES
    }


def validate_integrity(connection: duckdb.DuckDBPyConnection) -> None:
    """Cross-row constraints complement schema foreign keys and CHECK constraints."""
    invalid = connection.execute("""
        SELECT count(*) FROM refunds r JOIN orders o USING(order_id)
        LEFT JOIN order_items i ON r.order_item_id = i.order_item_id
        WHERE o.paid_at IS NULL OR r.created_at < o.paid_at
           OR i.order_id <> r.order_id
           OR r.refund_amount > i.quantity * i.unit_price - i.discount_amount
    """).fetchone()
    if invalid is None or invalid[0]:
        raise ValueError("Invalid refund/order relationship or amount")


def generate_demo_data(output: Path, config: DemoConfig = DemoConfig()) -> dict:
    """Create a new database; refuse to overwrite existing local data."""
    if output.exists():
        raise FileExistsError(f"Output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(config.seed)
    channels = [
        ("direct", "直接访问", "owned", False),
        ("search", "搜索", "search", True),
        ("social", "社交", "social", True),
        ("affiliate", "联盟", "partner", True),
    ]
    products = []
    for i in range(24):
        price = Decimal(30 + i * 19)
        products.append(
            (
                f"P{i:03}",
                f"SYN-{i:03}",
                f"合成商品{i:02}",
                ("electronics", "clothing", "home", "sports")[i % 4],
                f"SyntheticBrand{i % 4}",
                price * Decimal("0.55"),
                price,
                True,
            )
        )
    orders, items, refunds = [], [], []
    for month in (6, 7, 8):
        for channel, *_ in channels:
            count = config.orders_per_channel_month
            if month == 8 and channel == "social":
                count = int(count * config.social_aug_volume_ratio)
            for _ in range(count):
                oid = f"O{len(orders):06}"
                created = datetime(
                    2026,
                    month,
                    rng.randint(1, calendar.monthrange(2026, month)[1]),
                    rng.randrange(22),
                    rng.randrange(60),
                )
                cancel_prob = (
                    config.affiliate_aug_cancel_probability
                    if month == 8 and channel == "affiliate"
                    else config.base_cancel_probability
                )
                cancelled = rng.random() < cancel_prob
                paid = None if cancelled else created + timedelta(minutes=10)
                status = "cancelled" if cancelled else rng.choice(("paid", "completed"))
                orders.append(
                    (
                        oid,
                        f"C{rng.randrange(1500):04}",
                        channel,
                        rng.choice(("华东", "华南", "华北", "西部")),
                        status,
                        created,
                        paid,
                        created + timedelta(minutes=5) if cancelled else None,
                        "CNY",
                    )
                )
                for _ in range(rng.randint(1, 3)):
                    product = rng.choice(products)
                    iid = f"I{len(items):06}"
                    quantity = rng.randint(1, 3)
                    price = product[6]
                    discount = price * quantity * Decimal(rng.choice((0, 5, 10))) / 100
                    items.append((iid, oid, product[0], quantity, price, discount))
                    refund_prob = (
                        config.electronics_aug_refund_probability
                        if month == 8 and product[3] == "electronics"
                        else config.base_refund_probability
                    )
                    if not cancelled and rng.random() < refund_prob:
                        # One item-level request per item; same-day event stays inside the period.
                        refunds.append(
                            (
                                f"R{len(refunds):06}",
                                oid,
                                iid,
                                (price * quantity - discount) / 2,
                                rng.choice(("quality", "changed_mind", "damaged")),
                                rng.choices(("approved", "rejected", "pending"), weights=(8, 1, 1))[
                                    0
                                ],
                                created + timedelta(hours=1),
                            )
                        )
    metadata = {
        "generator_version": GENERATOR_VERSION,
        "schema_version": SCHEMA_VERSION,
        "seed": config.seed,
        "timezone": "Asia/Shanghai",
        "synthetic": True,
        "configuration": asdict(config),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    with duckdb.connect(str(output)) as connection:
        connection.execute("BEGIN")
        connection.execute(SCHEMA_SQL)
        for table, rows in zip(TABLES, (channels, products, orders, items, refunds), strict=True):
            if rows:
                connection.executemany(
                    f"INSERT INTO {table} VALUES ({','.join('?' for _ in rows[0])})", rows
                )
        connection.executemany(
            "INSERT INTO _demo_metadata VALUES (?, ?)",
            [(k, json.dumps(v, ensure_ascii=False)) for k, v in metadata.items()],
        )
        validate_integrity(connection)
        connection.execute("COMMIT")
        metadata["row_counts"] = {
            t: connection.execute(f"SELECT count(*) FROM {t}").fetchall()[0][0] for t in TABLES
        }
        metadata["fingerprints"] = fingerprints(connection)
    return metadata

"""Versioned synthetic commerce scenario; no real enterprise data."""

from dataclasses import dataclass

GENERATOR_VERSION = "0.1"
SCHEMA_VERSION = "0.1"
TABLES = ("channels", "products", "orders", "order_items", "refunds")


@dataclass(frozen=True)
class DemoConfig:
    seed: int = 20260910
    orders_per_channel_month: int = 500
    social_aug_volume_ratio: float = 0.4
    base_cancel_probability: float = 0.06
    affiliate_aug_cancel_probability: float = 0.30
    base_refund_probability: float = 0.04
    electronics_aug_refund_probability: float = 0.32

    def __post_init__(self) -> None:
        if self.orders_per_channel_month < 1:
            raise ValueError("orders_per_channel_month must be positive")
        for name, value in vars(self).items():
            if name.endswith(("probability", "ratio")) and not 0 <= value <= 1:
                raise ValueError(f"{name} must be between 0 and 1")

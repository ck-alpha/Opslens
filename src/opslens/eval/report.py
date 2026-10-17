"""Stable JSON serialization without absolute paths or wall-clock noise."""

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path


def normalize(value):
    if isinstance(value, Decimal):
        return format(value, "f")  # Preserve monetary precision; documented decimal strings.
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [normalize(v) for v in value]
    if isinstance(value, dict):
        return {k: normalize(v) for k, v in value.items()}
    return value


def write_report(report: dict, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(normalize(report), ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        + "\n",
        encoding="utf-8",
    )

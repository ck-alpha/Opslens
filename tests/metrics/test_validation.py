import hashlib
from pathlib import Path

import pytest

from opslens.metrics import MetricRegistry, validate_schema

DEFINITIONS = Path(__file__).parents[2] / "data/metrics/definitions"


def test_schema_read_only(demo_db):
    registry = MetricRegistry.from_directory(DEFINITIONS)
    before = hashlib.sha256(demo_db.read_bytes()).hexdigest()
    result = validate_schema(registry, demo_db)
    assert result.passed and result.errors == []
    assert hashlib.sha256(demo_db.read_bytes()).hexdigest() == before


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("source_tables", ["unknown"], "missing source table unknown"),
        ("source_columns", ["orders.nonexistent"], "missing source column orders.nonexistent"),
        (
            "time_semantics",
            {
                "time_column": "orders.nonexistent",
                "default_grain": "month",
                "allowed_grains": ["month"],
            },
            "missing time column orders.nonexistent",
        ),
        (
            "filters",
            [{"column": "orders.nonexistent", "operator": "EQ", "value": 1}],
            "missing filter column orders.nonexistent",
        ),
        (
            "allowed_dimensions",
            ["products.nonexistent"],
            "missing dimension column products.nonexistent",
        ),
    ],
)
def test_missing_schema(demo_db, field, value, error):
    registry = MetricRegistry.from_directory(DEFINITIONS)
    definition = registry.get("gross_gmv").model_dump()
    definition[field] = value
    from opslens.metrics import MetricDefinition

    modified = MetricRegistry([MetricDefinition.model_validate(definition)])
    result = validate_schema(modified, demo_db)
    assert not result.passed
    assert any(error in e for e in result.errors)

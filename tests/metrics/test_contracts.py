import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from opslens.metrics import MetricDefinition, MetricFilter, MetricRegistry, MetricTimeSemantics

ROOT = Path(__file__).parents[2]
DEFINITIONS = ROOT / "data/metrics/definitions"


@pytest.fixture
def registry():
    return MetricRegistry.from_directory(DEFINITIONS)


@pytest.fixture
def definition(registry):
    return registry.get("gross_gmv").model_dump()


@pytest.mark.parametrize(
    "change",
    [
        {"metric_id": "Bad ID"},
        {"version": "1"},
        {"version": "v1.0"},
        {"aliases": ["GMV", "ｇｍｖ"]},
        {"aliases": ["？"]},
        {"source_columns": []},
        {"source_columns": [""]},
        {"source_columns": ["paid_at"]},
        {"extra": True},
        {"metric_type": "DERIVED", "dependencies": []},
        {"dependencies": ["net_gmv"]},
        {"unit": "USD"},
        {"status": "active"},
        {"name": " "},
    ],
)
def test_invalid_definition(definition, change):
    definition.update(change)
    with pytest.raises(ValidationError):
        MetricDefinition.model_validate(definition)


@pytest.mark.parametrize(
    ("operator", "value"),
    [
        ("IN", "paid"),
        ("IN", []),
        ("NOT_IN", 1),
        ("IS_NULL", "x"),
        ("IS_NOT_NULL", []),
        ("EQ", ["paid"]),
        ("NEQ", None),
        ("LIKE", "x"),
        ("EQ", float("inf")),
    ],
)
def test_invalid_filter(operator, value):
    with pytest.raises(ValidationError):
        MetricFilter(column="orders.order_status", operator=operator, value=value)


@pytest.mark.parametrize(
    ("operator", "value"),
    [
        ("IN", ["paid"]),
        ("NOT_IN", [1, 2]),
        ("IS_NULL", None),
        ("IS_NOT_NULL", None),
        ("EQ", "12.30"),
        ("NEQ", 1),
    ],
)
def test_valid_filter(operator, value):
    MetricFilter(column="orders.order_status", operator=operator, value=value)


@pytest.mark.parametrize(
    "change",
    [
        {"default_grain": "year"},
        {"allowed_grains": []},
        {"allowed_grains": ["day"]},
        {"allowed_grains": ["month", "month"]},
        {"timezone": "Unknown/Zone"},
        {"time_column": "paid_at"},
    ],
)
def test_invalid_time(definition, change):
    time = definition["time_semantics"] | change
    with pytest.raises(ValidationError):
        MetricTimeSemantics.model_validate(time)


def test_real_registry(registry):
    assert len(registry.all()) == 9
    assert sum(d.status == "ACTIVE" for d in registry.all()) == 8
    assert registry.versions("gross_gmv") == ["0.9", "1.0"]
    assert registry.get("gross_gmv", "0.9").status == "DEPRECATED"
    assert registry.get("net_gmv").metric_type == "DERIVED"
    assert registry.get("missing") is None
    assert registry.get("gross_gmv", "9.0") is None
    assert registry.all() == MetricRegistry(list(reversed(registry.all()))).all()
    fingerprint = registry.fingerprint()
    registry.get("gross_gmv").aliases.append("mutation")
    assert registry.fingerprint() == fingerprint


def test_registry_rejections(registry):
    definitions = registry.all()
    with pytest.raises(ValueError, match="duplicate"):
        MetricRegistry(definitions + [definitions[0]])
    active = registry.get("gross_gmv")
    with pytest.raises(ValueError, match="multiple ACTIVE"):
        MetricRegistry(definitions + [active.model_copy(update={"version": "2.0"})])
    with pytest.raises(ValueError, match="missing active dependency"):
        MetricRegistry([d for d in definitions if d.metric_id != "paid_order_count"])
    with pytest.raises(ValueError, match="missing active dependency"):
        MetricRegistry(
            [
                d.model_copy(update={"status": "DEPRECATED"})
                if d.metric_id == "paid_order_count"
                else d
                for d in definitions
            ]
        )
    a = active.model_copy(
        update={"metric_id": "a", "metric_type": "DERIVED", "dependencies": ["b"]}
    )
    b = active.model_copy(
        update={"metric_id": "b", "metric_type": "DERIVED", "dependencies": ["a"]}
    )
    with pytest.raises(ValueError, match="cycle"):
        MetricRegistry([a, b])


def test_files_and_fingerprint(tmp_path, registry):
    with pytest.raises(ValueError, match="empty"):
        MetricRegistry.from_directory(tmp_path)
    (tmp_path / "ignored.txt").write_text("invalid")
    for i, definition in enumerate(reversed(registry.all())):
        (tmp_path / f"{i}.json").write_text(definition.model_dump_json(indent=4))
    assert MetricRegistry.from_directory(tmp_path).fingerprint() == registry.fingerprint()
    for path in tmp_path.glob("*.json"):
        path.write_text(json.dumps(json.loads(path.read_text()), sort_keys=True))
    assert MetricRegistry.from_directory(tmp_path).fingerprint() == registry.fingerprint()
    definitions = registry.all()
    definitions[0] = definitions[0].model_copy(update={"description": "Changed semantics"})
    assert MetricRegistry(definitions).fingerprint() != registry.fingerprint()
    (tmp_path / "duplicate.json").write_text(registry.all()[0].model_dump_json())
    with pytest.raises(ValueError, match="duplicate.json"):
        MetricRegistry.from_directory(tmp_path)
    (tmp_path / "broken.json").write_text('{"metric_id": "invalid"}')
    with pytest.raises(ValueError, match="broken.json"):
        MetricRegistry.from_directory(tmp_path)


def test_fingerprint_set_order_and_semantics(registry):
    definitions = registry.all()
    for definition in definitions:
        definition.aliases.reverse()
        definition.source_columns.reverse()
        definition.time_semantics.allowed_grains.reverse()
        for condition in definition.filters:
            if isinstance(condition.value, list):
                condition.value.reverse()
    assert MetricRegistry(definitions).fingerprint() == registry.fingerprint()
    definition = registry.get("gross_gmv")
    baseline = MetricRegistry([definition]).fingerprint()
    changed = definition.model_dump()
    changed["filters"][0]["value"] = ["paid"]
    assert MetricRegistry([MetricDefinition.model_validate(changed)]).fingerprint() != baseline


def test_numeric_versions(registry):
    definition = registry.get("gross_gmv")
    definitions = [
        definition.model_copy(update={"version": v, "status": "DEPRECATED"})
        for v in ["1.10", "1.2", "0.9"]
    ]
    assert MetricRegistry(definitions).versions("gross_gmv") == ["0.9", "1.2", "1.10"]

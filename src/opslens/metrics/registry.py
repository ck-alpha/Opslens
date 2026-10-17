"""Fail-fast registry with defensive copies and canonical semantic fingerprints."""

import hashlib
import json
from pathlib import Path

from opslens.metrics.models import MetricDefinition


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


class MetricRegistry:
    def __init__(self, definitions: list[MetricDefinition]):
        self._definitions: dict[tuple[str, str], MetricDefinition] = {}
        for definition in definitions:
            # Revalidate even model_copy/model_construct inputs and isolate mutable lists.
            definition = MetricDefinition.model_validate(definition.model_dump())
            key = (definition.metric_id, definition.version)
            if key in self._definitions:
                raise ValueError(f"duplicate metric/version: {key}")
            self._definitions[key] = definition
        if not self._definitions:
            raise ValueError("empty metric definitions")
        self.validate()

    @classmethod
    def from_directory(cls, path: Path) -> "MetricRegistry":
        definitions = []
        origins = []
        for file in sorted(path.glob("*.json")):
            if not file.is_file():
                continue
            try:
                definitions.append(MetricDefinition.model_validate_json(file.read_text("utf-8")))
            except ValueError as exc:
                raise ValueError(f"{file.name}: {exc}") from exc
            origins.append(file.name)
        try:
            return cls(definitions)
        except ValueError as exc:
            raise ValueError(f"definitions [{', '.join(origins)}]: {exc}") from exc

    def validate(self) -> None:
        ids = {d.metric_id for d in self._definitions.values()}
        for metric_id in sorted(ids):
            active = [
                d
                for d in self._definitions.values()
                if d.metric_id == metric_id and d.status == "ACTIVE"
            ]
            if len(active) > 1:
                raise ValueError(f"multiple ACTIVE versions: {metric_id}")
        # Every historical definition must also have resolvable active dependencies.
        for definition in self._definitions.values():
            for dependency in definition.dependencies:
                if dependency not in ids or self.active(dependency) is None:
                    raise ValueError(
                        f"{definition.metric_id}: missing active dependency {dependency}"
                    )
        visiting: set[tuple[str, str]] = set()
        visited: set[tuple[str, str]] = set()

        def visit(definition: MetricDefinition) -> None:
            key = (definition.metric_id, definition.version)
            if key in visiting:
                raise ValueError(f"dependency cycle: {definition.metric_id}")
            if key in visited:
                return
            visiting.add(key)
            for dependency in definition.dependencies:
                target = self.active(dependency)
                assert target is not None
                visit(target)
            visiting.remove(key)
            visited.add(key)

        for definition in self._definitions.values():
            visit(definition)

    def get(self, metric_id: str, version: str | None = None) -> MetricDefinition | None:
        if version is None:
            return self.active(metric_id)
        definition = self._definitions.get((metric_id, version))
        return definition.model_copy(deep=True) if definition else None

    def active(self, metric_id: str) -> MetricDefinition | None:
        definitions = [
            d
            for d in self._definitions.values()
            if d.metric_id == metric_id and d.status == "ACTIVE"
        ]
        if len(definitions) > 1:
            raise ValueError(f"multiple ACTIVE versions: {metric_id}")
        return definitions[0].model_copy(deep=True) if definitions else None

    def versions(self, metric_id: str) -> list[str]:
        return sorted(
            (v for m, v in self._definitions if m == metric_id), key=lambda v: (version_key(v), v)
        )

    def all(self) -> list[MetricDefinition]:
        return [
            self._definitions[k].model_copy(deep=True)
            for k in sorted(self._definitions, key=lambda k: (k[0], version_key(k[1]), k[1]))
        ]

    def fingerprint(self) -> str:
        canonical = []
        for definition in self.all():
            fields = definition.model_dump()
            for key in (
                "aliases",
                "dependencies",
                "source_tables",
                "source_columns",
                "allowed_dimensions",
            ):
                fields[key] = sorted(fields[key])
            fields["time_semantics"]["allowed_grains"].sort()
            for metric_filter in fields["filters"]:
                if isinstance(metric_filter["value"], list):
                    metric_filter["value"].sort()
            fields["filters"].sort(key=lambda f: json.dumps(f, sort_keys=True))
            canonical.append(fields)
        serialized = json.dumps(
            canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

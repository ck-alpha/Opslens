"""Deterministic phrase resolution. No clarification text, SQL, or LLM calls."""

import re
from typing import TypedDict

from opslens.metrics.models import MetricCandidate, MetricResolution
from opslens.metrics.normalization import normalize_text, phrase_spans
from opslens.metrics.registry import MetricRegistry


class _QueryFields(TypedDict):
    query: str
    normalized_query: str


class MetricResolver:
    def __init__(self, registry: MetricRegistry):
        self.registry = registry

    def resolve(self, query: str, version: str | None = None) -> MetricResolution:
        normalized = normalize_text(query)
        common: _QueryFields = {"query": query, "normalized_query": normalized}
        try:
            self.registry.validate()
        except ValueError as exc:
            reason = (
                "MULTIPLE_ACTIVE_VERSIONS" if "multiple ACTIVE" in str(exc) else "INVALID_REGISTRY"
            )
            return MetricResolution(**common, status="CONFLICT", reason_code=reason)
        versions = re.findall(r"(?:@|(?<![a-z0-9_])v)([0-9]+\.[0-9]+)(?![a-z0-9_.])", normalized)
        requested = set(versions)
        if version is not None:
            if not re.fullmatch(r"[0-9]+\.[0-9]+", version):
                return MetricResolution(**common, status="UNKNOWN", reason_code="INVALID_VERSION")
            requested.add(version)
        if len(requested) > 1:
            return MetricResolution(**common, status="CONFLICT", reason_code="VERSION_CONFLICT")
        version = next(iter(requested), None)
        text = re.sub(r"(?:@|(?<![a-z0-9_])v)[0-9]+\.[0-9]+(?![a-z0-9_.])", " ", normalized)
        # Explicit but malformed version syntax must never silently select ACTIVE.
        if re.search(r"@|(?<![a-z0-9_])v[0-9]", text):
            return MetricResolution(**common, status="UNKNOWN", reason_code="INVALID_VERSION")
        matches: set[tuple[int, int, int, str, str]] = set()
        for definition in self.registry.all():
            # Historical aliases do not influence default resolution of active metrics.
            if version is None and definition.status != "ACTIVE":
                if self.registry.active(definition.metric_id) is not None:
                    continue
            phrases = [(definition.metric_id, 0), (definition.name, 1)]
            phrases += [(alias, 2) for alias in definition.aliases]
            for phrase, priority in phrases:
                for start, end in phrase_spans(phrase, text):
                    matches.add((start, end, priority, definition.metric_id, text[start:end]))
        # Suppress contained shorter phrases, not independent mentions elsewhere.
        retained = [
            m
            for m in matches
            if not any(
                n[0] <= m[0] and n[1] >= m[1] and (n[1] - n[0] > m[1] - m[0]) for n in matches
            )
        ]
        groups: dict[tuple[int, int], list[tuple[int, str, str]]] = {}
        for start, end, priority, metric_id, phrase in sorted(retained):
            groups.setdefault((start, end), []).append((priority, metric_id, phrase))
        ids: set[str] = set()
        priorities = []
        phrases_found = []
        mention_sets = []
        for entries in groups.values():
            priority = min(e[0] for e in entries)
            selected = {e[1] for e in entries if e[0] == priority}
            ids.update(selected)
            mention_sets.append(selected)
            priorities.append(priority)
            phrases_found.append(entries[0][2])
        if not ids:
            return MetricResolution(**common, status="UNKNOWN", reason_code="NO_METRIC_MATCH")
        candidates = []
        for metric_id in sorted(ids):
            try:
                definition = self.registry.get(metric_id, version)
            except ValueError:
                return MetricResolution(
                    **common, status="CONFLICT", reason_code="MULTIPLE_ACTIVE_VERSIONS"
                )
            if definition is None and version is None:
                historical = self.registry.versions(metric_id)
                definition = self.registry.get(metric_id, historical[-1])
            if definition is not None:
                candidates.append(
                    MetricCandidate(
                        metric_id=metric_id,
                        version=definition.version,
                        name=definition.name,
                        status=definition.status,
                    )
                )
        matched_text = " | ".join(dict.fromkeys(phrases_found))
        if not candidates:
            return MetricResolution(
                **common,
                status="UNKNOWN",
                matched_text=matched_text,
                reason_code="VERSION_NOT_FOUND",
            )
        if len(ids) > 1:
            reason = "MULTIPLE_METRIC_MENTIONS" if len(mention_sets) > 1 else "AMBIGUOUS_ALIAS"
            return MetricResolution(
                **common,
                status="AMBIGUOUS",
                matched_text=matched_text,
                candidates=candidates,
                reason_code=reason,
            )
        candidate = candidates[0]
        if candidate.status == "DEPRECATED":
            status, reason = "DEPRECATED", "DEPRECATED_VERSION"
        else:
            status = "RESOLVED"
            reason = (
                "EXACT_METRIC_ID"
                if min(priorities) == 0
                else "EXACT_CANONICAL_NAME"
                if min(priorities) == 1
                else "LONGEST_ALIAS"
                if len(retained) < len(matches)
                else "UNIQUE_ALIAS"
            )
        return MetricResolution(
            **common,
            status=status,
            matched_text=matched_text,
            metric_id=candidate.metric_id,
            version=candidate.version,
            candidates=candidates,
            reason_code=reason,
        )

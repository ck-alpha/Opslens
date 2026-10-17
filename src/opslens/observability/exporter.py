"""Strict JSON / JSONL exporters, with identical redaction enforcement."""

import json

from opslens.observability.models import AggregateObservation, RunObservation
from opslens.observability.redaction import assert_safe


def export_json(observation: RunObservation | AggregateObservation) -> str:
    if type(observation) not in (RunObservation, AggregateObservation):
        raise ValueError("OBSERVABILITY_MODEL_NOT_ALLOWED")
    # Revalidate even model_copy/model_construct supplied by callers.
    observation = type(observation).model_validate_json(observation.model_dump_json())
    assert_safe(observation)
    return json.dumps(observation.model_dump(mode="json"), sort_keys=True, allow_nan=False)


def export_jsonl(observations: list[RunObservation]) -> str:
    return "".join(export_json(o) + "\n" for o in observations)


def read_jsonl(value: str) -> list[RunObservation]:
    observations = [
        RunObservation.model_validate_json(line) for line in value.splitlines() if line.strip()
    ]
    for observation in observations:
        assert_safe(observation)
    return observations

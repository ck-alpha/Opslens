"""Adversarial contracts: integrity, correlation, privacy and truthful timing."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

from opslens.observability.exporter import export_json
from opslens.observability.metrics import quantiles, summarize
from opslens.observability.models import RunObservation
from opslens.observability.service import ObservabilityService, duration
from opslens.runtime.failures import RuntimeFault
from opslens.runtime.models import ArtifactView, EventView, RunView, StepView, TaskView

NOW = datetime(2026, 9, 13, tzinfo=timezone.utc)


def repository():
    repo = Mock()
    repo.load_run.return_value = RunView(
        run_id="run",
        task_id="task",
        attempt_no=1,
        status="PENDING",
        runtime_policy_id="runtime",
        runtime_policy_version="1.0",
        runtime_policy_fingerprint="a" * 64,
        runtime_context_fingerprint="b" * 64,
        created_at=NOW,
        started_at=None,
        finished_at=None,
        failure_class=None,
        failure_code=None,
    )
    repo.load_task.return_value = TaskView(
        task_id="task",
        idempotency_key="DO_NOT_EXPORT",
        request_fingerprint="c" * 64,
        request_payload={
            "candidate_sql": "SECRET_SQL",
            "password": "SECRET_PASSWORD",
            "rows": ["SECRET_ROW"],
        },
        status="PENDING",
        created_at=NOW,
        updated_at=NOW,
    )
    repo.list_steps.return_value = [
        StepView(
            step_id="step",
            run_id="run",
            step_key="execute_with_evidence",
            step_type="EXECUTE_WITH_EVIDENCE",
            ordinal=1,
            status="PENDING",
            input_fingerprint=None,
            attempt_count=0,
            last_failure_class=None,
            last_error_code=None,
            started_at=None,
            finished_at=None,
        )
    ]
    repo.load_events.return_value = []
    repo.load_step_artifacts.return_value = []
    return repo


@pytest.mark.parametrize(
    "values,expected",
    [
        ([], (None, None, 0)),
        ([1.0], (1.0, 1.0, 1)),
        ([1.0, 2.0], (1.0, 2.0, 2)),
        ([float(i) for i in range(1, 101)], (50.0, 95.0, 100)),
        ([None, 0.0], (0.0, 0.0, 1)),
    ],
)
def test_nearest_rank(values, expected):
    result = quantiles(values)
    assert (result.p50, result.p95, result.sample_count) == expected


@pytest.mark.parametrize("value", [-1.0, float("nan"), float("inf")])
def test_invalid_timing_rejected(value):
    with pytest.raises(ValueError):
        quantiles([value])


def test_empty_aggregate_and_unknown_usage():
    value = summarize([])
    assert value.retry_rate.rate is None
    assert value.run_latency.sample_count == 0
    assert value.usage.accounting_mode == "UNKNOWN" and value.usage.cost_usd is None


def test_pending_m7_rows_read_without_running_or_leaking():
    repo = repository()
    obs = ObservabilityService(repo).get_run("run")
    assert obs.timings.run_wall_clock_ms is None
    assert obs.usage.accounting_mode == "UNKNOWN"
    encoded = export_json(obs)
    assert not any(
        s in encoded for s in ["SECRET", "DO_NOT_EXPORT", "candidate_sql", "request_payload"]
    )
    assert RunObservation.model_validate_json(encoded) == obs
    assert set(c[0] for c in repo.method_calls) <= {
        "load_run",
        "load_task",
        "list_steps",
        "load_events",
        "load_step_artifacts",
    }


@pytest.mark.parametrize(
    "target",
    [
        "task",
        "run",
        "step",
        "artifact_run",
        "artifact_step",
        "artifact_task",
        "event_run",
        "event_step",
    ],
)
def test_correlation_rejected(target):
    repo = repository()
    if target in ["task", "run", "step"]:
        getter = {"task": "load_task", "run": "load_run", "step": "list_steps"}[target]
        value = getattr(repo, getter).return_value
        if target == "step":
            getattr(repo, getter).return_value = [value[0].model_copy(update={"run_id": "other"})]
        else:
            getattr(repo, getter).return_value = value.model_copy(
                update={"task_id" if target == "task" else "run_id": "other"}
            )
    elif target.startswith("artifact"):
        artifact = ArtifactView(
            artifact_id="artifact",
            task_id="task",
            run_id="run",
            step_id="step",
            artifact_key="key",
            artifact_type="governed_sql_result",
            schema_version="1.0",
            content_fingerprint="a" * 64,
            payload={},
            created_at=NOW,
        )
        repo.load_step_artifacts.return_value = [
            artifact.model_copy(update={target.split("_")[1] + "_id": "other"})
        ]
    else:
        event = EventView(
            event_id="event",
            run_id="run",
            step_id="step",
            sequence=1,
            event_type="STEP_STARTED",
            payload={},
            event_fingerprint="a" * 64,
            created_at=NOW,
        )
        repo.load_events.return_value = [
            event.model_copy(update={target.split("_")[1] + "_id": "other"})
        ]
    with pytest.raises(RuntimeFault, match="TRACE_CORRELATION_ERROR"):
        ObservabilityService(repo).get_run("run")


def test_artifact_codec_is_not_bypassed():
    repo = repository()
    repo.load_step_artifacts.return_value = [
        ArtifactView(
            artifact_id="artifact",
            task_id="task",
            run_id="run",
            step_id="step",
            artifact_key="key",
            artifact_type="governed_sql_result",
            schema_version="1.0",
            content_fingerprint="bad",
            payload={},
            created_at=NOW,
        )
    ]
    with pytest.raises(RuntimeFault, match="ARTIFACT_INTEGRITY_ERROR"):
        ObservabilityService(repo).get_run("run")


def test_event_integrity_is_not_bypassed():
    repo = repository()
    repo.load_events.side_effect = RuntimeFault("EVENT_INTEGRITY_ERROR")
    with pytest.raises(RuntimeFault, match="EVENT_INTEGRITY_ERROR"):
        ObservabilityService(repo).get_run("run")


def test_observation_rejects_changed_snapshot():
    repo = repository()
    run = repo.load_run.return_value
    repo.load_run.side_effect = [run, run.model_copy(update={"status": "RUNNING"})]
    with pytest.raises(RuntimeFault, match="OBSERVATION_SNAPSHOT_CHANGED"):
        ObservabilityService(repo).get_run("run")


@pytest.mark.parametrize(
    "value",
    [
        "DELETE FROM orders",
        "postgresql://user:password@host/db",
        "/home/private/file",
        "Traceback (most recent call last):",
        "Bearer abcdef",
        "sk-proj-PRIVATE",
    ],
)
def test_scalar_injection_cannot_escape(value):
    obs = ObservabilityService(repository()).get_run("run")
    with pytest.raises(ValueError):
        export_json(obs.model_copy(update={"failure_code": value}))


def test_elapsed_is_last_attempt_not_total():
    assert duration(NOW, NOW + timedelta(seconds=2)) == 2000
    assert duration(NOW, None) is None
    with pytest.raises(ValueError):
        duration(NOW, NOW - timedelta(seconds=1))


def test_no_eval_dependency_in_production():
    for folder in ["runtime", "observability"]:
        for path in Path("src/opslens", folder).glob("*.py"):
            source = path.read_text()
            assert "opslens.eval" not in source
            assert "reference_sql" not in source and "expected_behavior" not in source


def test_aggregate_duplicate_run_rejected():
    with pytest.raises(ValueError, match="DUPLICATE_RUN_ID"):
        ObservabilityService(repository()).summarize_runs(["run", "run"])


def test_export_extra_fields_rejected():
    obs = ObservabilityService(repository()).get_run("run")
    data = obs.model_dump(mode="json")
    data["candidate_sql"] = "SELECT secret"
    with pytest.raises(ValueError):
        RunObservation.model_validate_json(json.dumps(data))


def test_export_rejects_subclass_extending_allowlist():
    class ExpandedObservation(RunObservation):
        rows: list[int]

    obs = ObservabilityService(repository()).get_run("run")
    data = obs.model_dump(mode="json")
    data["rows"] = [123]
    expanded = ExpandedObservation.model_validate_json(json.dumps(data))
    with pytest.raises(ValueError, match="MODEL_NOT_ALLOWED"):
        export_json(expanded)

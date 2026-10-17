"""Production runtime records remain readable without replay or telemetry migration."""

import os
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest

from opslens.observability import ObservabilityService
from opslens.observability.exporter import export_json, export_jsonl, read_jsonl
from opslens.persistence.database import database_engine, require_test_database
from opslens.persistence.repository import Repository
from opslens.runtime.engine import RuntimeEngine
from opslens.runtime.request import RuntimeAnalysisRequest
from opslens.runtime.steps import Pipeline


@pytest.mark.parametrize(
    "fixture,status",
    [
        ("gross_gmv_request.json", "SUCCEEDED"),
        ("wrong_metric_request.json", "FAILED"),
    ],
)
def test_real_m7_readonly_observation(demo_db, fixture, status):
    if not os.environ.get("OPSLENS_RUNTIME_DATABASE_URL"):
        pytest.skip("PostgreSQL integration requires runtime database environment")
    engine = database_engine()
    require_test_database(engine)
    try:
        pipeline = Pipeline.build(demo_db)
        request = RuntimeAnalysisRequest.model_validate_json(
            Path("data/runtime", fixture).read_text()
        )
        run = RuntimeEngine(engine, pipeline).start(str(uuid4()), request)
        assert run.status == status
        repo = Repository(engine)
        before = (
            repo.load_run(run.run_id),
            repo.load_events(run.run_id),
            repo.list_steps(run.run_id),
        )
        # Reading observations must not touch SQL execution or validation.
        with patch.object(
            pipeline.evidence.governed, "execute", side_effect=AssertionError("REPLAY")
        ):
            service = ObservabilityService(repo)
            observation = service.get_run(run.run_id)
            aggregate = service.summarize_runs([run.run_id])
        assert observation.metric_id == "gross_gmv"
        assert observation.evidence_fingerprint and observation.validation_fingerprint
        assert observation.report_fingerprint and observation.query_fingerprint
        assert observation.policy_decision == "ALLOW"
        assert aggregate.run_count == 1 and aggregate.run_latency.sample_count == 1
        assert aggregate.time_to_verified_answer.sample_count == (1 if status == "SUCCEEDED" else 0)
        assert "SELECT" not in export_json(observation)
        assert read_jsonl(export_jsonl([observation])) == [observation]
        assert before == (
            repo.load_run(run.run_id),
            repo.load_events(run.run_id),
            repo.list_steps(run.run_id),
        )
    finally:
        engine.dispose()

"""Real PostgreSQL tests; no SQLite substitute for locking or trigger semantics."""

import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import inspect, text

from alembic import command
from opslens.eval.runtime_models import RuntimeCase
from opslens.eval.runtime_runner import Scenario
from opslens.persistence.database import database_engine, require_test_database
from opslens.runtime.failures import RuntimeFault

CASES = [
    RuntimeCase.model_validate_json(line)
    for line in Path("tests/fixtures/runtime.jsonl")
    .read_text()
    .splitlines()
]


@pytest.fixture(scope="module")
def database():
    if not os.environ.get("OPSLENS_RUNTIME_DATABASE_URL"):
        pytest.skip("OPSLENS_RUNTIME_DATABASE_URL required for real PostgreSQL integration")
    engine = database_engine()
    require_test_database(engine)
    command.upgrade(Config("alembic.ini"), "head")
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def analysis_db(tmp_path_factory):
    from opslens.demo.config import DemoConfig
    from opslens.demo.generator import generate_demo_data

    path = tmp_path_factory.mktemp("runtime") / "analysis.duckdb"
    generate_demo_data(path, DemoConfig())
    return path


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.case_id + "-" + c.scenario)
def test_postgres_scenario(database, analysis_db, case):
    result = Scenario(database, analysis_db, case).run()
    assert result["matched"], result


def test_migration_roundtrip_isolated_schema(database):
    # Dedicated empty schema, explicit test-name guard, no destructive shared reset.
    require_test_database(database)
    schema = "m7_" + uuid4().hex
    with database.begin() as conn:
        conn.execute(text(f"CREATE SCHEMA {schema}"))
    original = os.environ["OPSLENS_RUNTIME_DATABASE_URL"]
    from sqlalchemy.engine import make_url

    url = make_url(original).update_query_dict({"options": f"-csearch_path={schema}"})
    os.environ["OPSLENS_RUNTIME_DATABASE_URL"] = url.render_as_string(hide_password=False)
    isolated = database_engine()
    try:
        config = Config("alembic.ini")
        command.upgrade(config, "head")
        assert set(inspect(isolated).get_table_names()) == {
            "tasks",
            "runs",
            "steps",
            "runtime_events",
            "runtime_artifacts",
            "alembic_version",
            "product_events",
        }
        assert inspect(isolated).get_check_constraints("steps")
        with isolated.connect() as conn:
            assert (
                conn.scalar(
                    text(
                        "SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
                        "JOIN pg_namespace n ON n.oid=c.relnamespace "
                        "WHERE NOT t.tgisinternal AND n.nspname=:schema"
                    ),
                    {"schema": schema},
                )
                == 4
            )
        command.downgrade(config, "base")
        assert inspect(isolated).get_table_names() == ["alembic_version"]
        command.upgrade(config, "head")
        command.check(config)
    finally:
        os.environ["OPSLENS_RUNTIME_DATABASE_URL"] = original
        isolated.dispose()
        with database.begin() as conn:
            conn.execute(text(f"DROP SCHEMA {schema} CASCADE"))


def test_destructive_guard():
    from sqlalchemy import create_engine

    engine = create_engine("postgresql+psycopg:///production")
    with pytest.raises(RuntimeFault, match="DESTRUCTIVE_TEST_DATABASE_GUARD"):
        require_test_database(engine)


def test_lost_lock_connection_cannot_continue(database, analysis_db):
    from opslens.persistence.repository import Repository
    from opslens.runtime.engine import RuntimeEngine
    from opslens.runtime.request import RuntimeAnalysisRequest
    from opslens.runtime.steps import Pipeline

    pipeline = Pipeline.build(analysis_db)
    runtime = RuntimeEngine(database, pipeline)
    _, run = runtime.submit(
        str(uuid4()), RuntimeAnalysisRequest(candidate_sql="SELECT order_id FROM orders LIMIT 2")
    )

    def disconnect(point, step):
        if point == "after_checkpoint" and step.ordinal == 1:
            with database.begin() as connection:
                # Only the other session holding this test run's advisory lock.
                from opslens.runtime.locking import lock_key

                key = lock_key(run.run_id) & ((1 << 64) - 1)
                assert connection.scalar(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_locks "
                        "WHERE locktype='advisory' AND classid=:hi AND objid=:lo "
                        "AND pid<>pg_backend_pid()"
                    ),
                    {"hi": key >> 32, "lo": key & 0xFFFFFFFF},
                )

    with pytest.raises(RuntimeFault, match="RUNTIME_STORAGE_UNAVAILABLE"):
        RuntimeEngine(database, pipeline, hook=disconnect).resume(run.run_id)
    repo = Repository(database)
    assert [s.attempt_count for s in repo.list_steps(run.run_id)] == [1, 0]
    assert runtime.resume(run.run_id).status == "SUCCEEDED"
    assert [s.attempt_count for s in repo.list_steps(run.run_id)] == [1, 1]

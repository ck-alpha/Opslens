"""Explicit evaluation-only subprocess fault injection; never imported by runtime."""

import argparse
import os
import time
from pathlib import Path

from opslens.persistence.database import database_engine, require_test_database
from opslens.runtime.engine import RuntimeEngine
from opslens.runtime.models import StepView
from opslens.runtime.steps import Pipeline


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--analysis-db", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--point", default="none")
    parser.add_argument("--ordinal", type=int, default=1)
    parser.add_argument("--counter", type=Path, required=True)
    parser.add_argument("--ready", type=Path)
    args = parser.parse_args()
    database = database_engine()
    require_test_database(database)
    pipeline = Pipeline.build(args.analysis_db)
    execute = pipeline.evidence.governed.execute

    def counted(sql: str):
        with args.counter.open("a") as stream:
            stream.write("SQL_EXECUTED\n")
            stream.flush()
            os.fsync(stream.fileno())
        return execute(sql)

    pipeline.evidence.governed.execute = counted

    def hook(point: str, step: StepView) -> None:
        if args.point == "hold" and point == "before_execute" and step.ordinal == args.ordinal:
            assert args.ready is not None
            args.ready.write_text("ready")
            time.sleep(45)
        if point == args.point and step.ordinal == args.ordinal:
            os._exit(73)

    run = RuntimeEngine(database, pipeline, hook=hook).resume(args.run_id)
    print(run.status)
    raise SystemExit(0 if run.status == "SUCCEEDED" else 2)


if __name__ == "__main__":
    main()

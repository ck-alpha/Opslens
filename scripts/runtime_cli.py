"""Operate durable runs. Connection configuration is read only from the environment."""

import argparse
import json
from pathlib import Path

from opslens.persistence.database import database_engine
from opslens.persistence.repository import Repository
from opslens.runtime.codec import decode
from opslens.runtime.engine import RuntimeEngine
from opslens.runtime.request import RuntimeAnalysisRequest
from opslens.runtime.steps import Pipeline


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    for command in ("start", "resume", "rerun", "status", "events", "artifacts"):
        sub = subs.add_parser(command)
        if command in ("start", "resume", "rerun"):
            sub.add_argument("--analysis-db", type=Path, required=True)
        if command == "start":
            sub.add_argument("--request", type=Path, required=True)
            sub.add_argument("--idempotency-key", required=True)
        elif command == "rerun":
            sub.add_argument("--task-id", required=True)
        else:
            sub.add_argument("--run-id", required=True)
        if command == "artifacts":
            sub.add_argument("--show-payload", action="store_true")
    args = parser.parse_args()
    database = database_engine()
    repo = Repository(database)
    try:
        if args.command in ("start", "resume", "rerun"):
            runtime = RuntimeEngine(database, Pipeline.build(args.analysis_db))
            if args.command == "start":
                request = RuntimeAnalysisRequest.model_validate_json(
                    args.request.read_text("utf-8")
                )
                run = runtime.start(args.idempotency_key, request)
            elif args.command == "rerun":
                run = runtime.rerun(args.task_id)
            else:
                run = runtime.resume(args.run_id)
            print(run.model_dump_json(indent=2))
        elif args.command == "events":
            print(
                json.dumps(
                    [e.model_dump(mode="json") for e in repo.load_events(args.run_id)], indent=2
                )
            )
        else:
            steps = repo.list_steps(args.run_id)
            artifacts = [a for s in steps for a in repo.load_step_artifacts(s.step_id)]
            for a in artifacts:
                decode(a.artifact_type, a.schema_version, a.payload, a.content_fingerprint)
            values = [
                a.model_dump(
                    mode="json",
                    exclude=set()
                    if args.command == "artifacts" and args.show_payload
                    else {"payload"},
                )
                for a in artifacts
            ]
            result = (
                values
                if args.command == "artifacts"
                else {
                    "run": repo.load_run(args.run_id).model_dump(mode="json"),
                    "steps": [s.model_dump(mode="json") for s in steps],
                    "artifacts": values,
                }
            )
            print(json.dumps(result, indent=2))
    except Exception as exc:
        # Neither request validation details nor DB exception messages reach stdout.
        from opslens.runtime.failures import RuntimeFault

        print(
            json.dumps(
                {
                    "error_code": exc.code
                    if isinstance(exc, RuntimeFault)
                    else "RUNTIME_COMMAND_FAILED",
                    "error_type": type(exc).__name__,
                }
            )
        )
        raise SystemExit(1) from None
    finally:
        database.dispose()


if __name__ == "__main__":
    main()

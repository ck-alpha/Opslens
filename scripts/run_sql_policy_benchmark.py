"""Run the independent SQL governance suite without a model or external SQL network access."""

import argparse
from pathlib import Path

import duckdb

from opslens.eval.report import write_report
from opslens.eval.sql_policy_runner import run_sql_policy_benchmark


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/demo/generated/opslens_demo.duckdb"))
    parser.add_argument("--policy", type=Path, default=Path("data/sql_policy/default.v1.json"))
    parser.add_argument(
        "--cases", type=Path, default=Path("tests/fixtures/sql_policy.jsonl")
    )
    parser.add_argument(
        "--output", type=Path, default=Path("data/demo/generated/sql_policy_report.json")
    )
    args = parser.parse_args()
    try:
        report = run_sql_policy_benchmark(args.db, args.policy, args.cases)
    except (OSError, ValueError, duckdb.Error) as exc:
        write_report(
            {
                "report_type": "governed_sql_policy",
                "passed": False,
                "error_type": type(exc).__name__,
            },
            args.output,
        )
        raise SystemExit(1) from None
    write_report(report, args.output)
    print({k: v for k, v in report.items() if k != "results"})
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

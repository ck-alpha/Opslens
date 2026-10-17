"""Generate synthetic data without a model, credentials, or network access."""

import argparse
import json
from pathlib import Path

from opslens.demo.config import DemoConfig
from opslens.demo.generator import generate_demo_data


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=20260910)
    parser.add_argument(
        "--output", type=Path, default=Path("data/demo/generated/opslens_demo.duckdb")
    )
    args = parser.parse_args()
    print(
        json.dumps(
            generate_demo_data(args.output, DemoConfig(seed=args.seed)),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

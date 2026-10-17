"""Summarize a required UTC window (maximum 31 days); no unique-user claims."""

import argparse
import json
from datetime import datetime, timezone

from opslens.persistence.database import database_engine
from opslens.product.events import ProductEventRepository
from opslens.product.metrics import summarize_events


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", required=True)
    parser.add_argument("--until")
    args = parser.parse_args()
    database = None
    try:
        database = database_engine()
        result = summarize_events(
            ProductEventRepository(database),
            datetime.fromisoformat(args.since),
            datetime.fromisoformat(args.until) if args.until else datetime.now(timezone.utc),
        )
        print(json.dumps(result, indent=2))
    except Exception:
        print('{"code":"PRODUCT_METRICS_FAILED"}')
        raise SystemExit(1) from None
    finally:
        if database is not None:
            database.dispose()


if __name__ == "__main__":
    main()

"""PostgreSQL-only engine construction without URL logging or implicit schema changes."""

import os

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.pool import NullPool

from opslens.runtime.failures import RuntimeFault


def database_engine(url: str | None = None) -> Engine:
    value = url or os.environ.get("OPSLENS_RUNTIME_DATABASE_URL", "")
    try:
        parsed = make_url(value)
        if parsed.drivername != "postgresql+psycopg":
            raise ValueError
        return create_engine(parsed, poolclass=NullPool, hide_parameters=True, echo=False)
    except Exception:
        raise RuntimeFault("INVALID_RUNTIME_DATABASE_CONFIGURATION") from None


def require_test_database(engine: Engine) -> None:
    if not (engine.url.database or "").endswith("_test"):
        raise RuntimeFault("DESTRUCTIVE_TEST_DATABASE_GUARD")

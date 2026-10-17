"""Explicit startup, deterministic demo lifecycle, and safe readiness checks."""

import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path

import duckdb
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import URL

from opslens.demo.config import GENERATOR_VERSION, SCHEMA_VERSION, DemoConfig
from opslens.demo.generator import fingerprints, generate_demo_data, validate_integrity
from opslens.knowledge.index import BusinessKnowledgeIndex
from opslens.persistence.database import database_engine
from opslens.product.service import ProductService
from opslens.runtime.engine import RuntimeEngine
from opslens.runtime.lumen_baseline import LUMEN_BASELINE_COMMIT
from opslens.runtime.steps import Pipeline


def configure_runtime_url() -> None:
    """Compose credentials stay in process environment; never print the resulting URL."""
    if not os.getenv("OPSLENS_RUNTIME_DATABASE_URL") and os.getenv("OPSLENS_POSTGRES_PASSWORD"):
        os.environ["OPSLENS_RUNTIME_DATABASE_URL"] = URL.create(
            "postgresql+psycopg",
            username="opslens",
            password=os.environ["OPSLENS_POSTGRES_PASSWORD"],
            host="postgres",
            port=5432,
            database="opslens_runtime",
        ).render_as_string(hide_password=False)


@dataclass(frozen=True)
class ProductConfig:
    analysis_db: Path = Path("data/demo/generated/opslens_demo.duckdb")
    data: Path = Path("data")
    demo_mode: bool = True
    host: str = "127.0.0.1"
    port: int = 5006
    origins: tuple[str, ...] = ("localhost:5006", "127.0.0.1:5006")
    revision: str = "UNKNOWN"

    @classmethod
    def from_env(cls):
        configure_runtime_url()
        port = int(os.getenv("OPSLENS_UI_PORT", "5006"))
        origins = tuple(
            v.strip()
            for v in os.getenv(
                "OPSLENS_ALLOWED_WS_ORIGINS", f"localhost:{port},127.0.0.1:{port}"
            ).split(",")
        )
        if (
            not 1 <= port <= 65535
            or not origins
            or any(not re.fullmatch(r"[a-zA-Z0-9.-]+(?::[0-9]{1,5})?", v) for v in origins)
        ):
            raise ValueError("INVALID_UI_LISTENER_CONFIGURATION")
        revision = os.getenv("OPSLENS_BUILD_REVISION", "UNKNOWN")
        if not re.fullmatch(r"[a-fA-F0-9]{7,40}|UNKNOWN", revision):
            revision = "UNKNOWN"
        mode = os.getenv("OPSLENS_DEMO_MODE", "true").lower()
        if mode not in ("true", "false"):
            raise ValueError("INVALID_DEMO_MODE")
        return cls(
            analysis_db=Path(os.getenv("OPSLENS_ANALYSIS_DB", str(cls.analysis_db))),
            demo_mode=mode == "true",
            host=os.getenv("OPSLENS_UI_HOST", cls.host),
            port=port,
            origins=origins,
            revision=revision,
        )


def ensure_demo(config: ProductConfig, *, generate: bool = False) -> None:
    if not config.analysis_db.exists():
        if not generate or not config.demo_mode:
            raise ValueError("ANALYSIS_DB_MISSING")
        config.analysis_db.parent.mkdir(parents=True, exist_ok=True)
        generate_demo_data(config.analysis_db, DemoConfig())
    expected = json.loads((config.data / "product/demo_manifest.json").read_text())
    with duckdb.connect(str(config.analysis_db), read_only=True) as connection:
        metadata = {
            k: json.loads(v)
            for k, v in connection.execute("SELECT key, value FROM _demo_metadata").fetchall()
        }
        required = dict(
            generator_version=GENERATOR_VERSION,
            schema_version=SCHEMA_VERSION,
            seed=20260910,
            timezone="Asia/Shanghai",
            synthetic=True,
            configuration=asdict(DemoConfig()),
        )
        if any(metadata.get(k) != v for k, v in required.items()):
            raise ValueError("DEMO_METADATA_MISMATCH")
        if fingerprints(connection) != expected["table_fingerprints"]:
            raise ValueError("DEMO_FINGERPRINT_MISMATCH")
        validate_integrity(connection)


async def build_product_service(config: ProductConfig) -> ProductService:
    ensure_demo(config)
    pipeline = Pipeline.build(config.analysis_db, config.data)
    index = BusinessKnowledgeIndex(pipeline.validation.builder.corpus)
    await index.build()
    return ProductService(RuntimeEngine(database_engine(), pipeline), index, config.data)


def readiness(config: ProductConfig) -> dict:
    database = None
    try:
        database = database_engine()
        with database.connect() as connection:
            connection.execute(text("SELECT 1"))
            heads = MigrationContext.configure(connection).get_current_heads()
            expected = ScriptDirectory.from_config(Config("alembic.ini")).get_heads()
            if set(heads) != set(expected):
                raise ValueError("MIGRATION_NOT_AT_HEAD")
        ensure_demo(config)
        pipeline = Pipeline.build(config.analysis_db, config.data)
        from opslens.product.recipes import load_recipes

        load_recipes(config.data / "product/demo_recipes")
        from opslens.runtime.policy import RuntimePolicy

        policies = [pipeline.validation.sql_policy, pipeline.validation.policy, RuntimePolicy()]
        return {
            "ready": True,
            "code": "READY",
            "database": "HEALTHY",
            "analysis_db": "HEALTHY",
            "app_revision": config.revision[:12],
            "lumen_sha": LUMEN_BASELINE_COMMIT,
            "metric_registry_fingerprint": pipeline.validation.builder.registry.fingerprint(),
            "knowledge_corpus_fingerprint": pipeline.validation.builder.corpus.fingerprint,
            "policies": [
                {
                    "id": getattr(p, "policy_id", getattr(p, "runtime_id", "")),
                    "version": p.version,
                    "fingerprint": p.fingerprint(),
                }
                for p in policies
            ],
            "validation_scope": "LIMITED",
            "sql_generation_enabled": bool(os.getenv("OPSLENS_OLLAMA_MODEL")),
        }
    except Exception:
        return {
            "ready": False,
            "code": "READINESS_FAILED",
            "suggested_action": "Check operator configuration, migrations and demo integrity.",
        }
    finally:
        if database is not None:
            database.dispose()

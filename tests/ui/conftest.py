import asyncio
import os
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from opslens.demo.config import DemoConfig
from opslens.demo.generator import generate_demo_data
from opslens.knowledge.index import BusinessKnowledgeIndex
from opslens.persistence.database import database_engine, require_test_database
from opslens.product.service import ProductService
from opslens.runtime.engine import RuntimeEngine
from opslens.runtime.steps import Pipeline


@pytest.fixture(scope="module")
def product_service(tmp_path_factory):
    if not os.environ.get("OPSLENS_RUNTIME_DATABASE_URL"):
        pytest.skip("PostgreSQL required for product integration")
    database = database_engine()
    require_test_database(database)
    command.upgrade(Config("alembic.ini"), "head")
    path = tmp_path_factory.mktemp("product") / "analysis.duckdb"
    generate_demo_data(path, DemoConfig())
    pipeline = Pipeline.build(path)
    index = BusinessKnowledgeIndex(pipeline.validation.builder.corpus)
    asyncio.run(index.build())
    yield ProductService(RuntimeEngine(database, pipeline), index, Path("data"))
    database.dispose()

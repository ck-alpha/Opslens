"""Migrations use the environment URL without logging credentials."""

from alembic import context
from opslens.persistence.database import database_engine, require_test_database
from opslens.persistence.orm import Base
from opslens.product.events import ProductEventRow  # noqa: F401

engine = database_engine()
if context.get_x_argument(as_dictionary=True).get("destructive_test_reset") == "true":
    require_test_database(engine)
with engine.connect() as connection:
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()
engine.dispose()

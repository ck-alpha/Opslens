import pytest

from opslens.demo.config import DemoConfig
from opslens.demo.generator import generate_demo_data


@pytest.fixture(scope="session")
def demo_db(tmp_path_factory):
    path = tmp_path_factory.mktemp("demo") / "demo.duckdb"
    generate_demo_data(path, DemoConfig(orders_per_channel_month=40))
    return path

import json
import platform
import tomllib
from importlib.metadata import distribution, version
from pathlib import Path

import opslens
from opslens.runtime.lumen_baseline import LUMEN_BASELINE_COMMIT, get_lumen_runtime_info

EXPECTED_SHA = "c46a41aec7effba8ada8c41577fcc6432e7eeff3"


def test_package_and_exact_baseline():
    assert opslens.__version__ == version("opslens")
    assert LUMEN_BASELINE_COMMIT == EXPECTED_SHA


def test_runtime_info():
    assert get_lumen_runtime_info() == {
        "upstream_repository": "https://github.com/holoviz/lumen",
        "pinned_commit": EXPECTED_SHA,
        "installed_version": version("lumen"),
        "python_version": platform.python_version(),
    }


def test_packaging_matches_runtime_pin():
    project = tomllib.loads(
        (Path(__file__).parents[1] / "pyproject.toml").read_text()
    )["project"]
    assert project["requires-python"] == ">=3.11"
    assert [dep for dep in project["dependencies"] if dep.startswith("lumen ")] == [
        f"lumen @ git+https://github.com/holoviz/lumen.git@{EXPECTED_SHA}"
    ]


def test_installed_lumen_provenance():
    metadata = distribution("lumen").read_text("direct_url.json")
    assert metadata is not None, "Lumen must be installed from the pinned Git source"
    origin = json.loads(metadata)
    assert origin["url"] == "https://github.com/holoviz/lumen.git"
    assert origin["vcs_info"]["vcs"] == "git"
    assert origin["vcs_info"]["commit_id"] == EXPECTED_SHA

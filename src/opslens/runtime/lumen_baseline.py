"""Read baseline metadata without starting agents or making network requests."""

import platform
from importlib.metadata import PackageNotFoundError, version

LUMEN_REPOSITORY = "https://github.com/holoviz/lumen"
LUMEN_BASELINE_COMMIT = "c46a41aec7effba8ada8c41577fcc6432e7eeff3"


def get_lumen_runtime_info() -> dict[str, str]:
    """Return the declared pin and installed version (not proof of installed SHA)."""
    try:
        installed = version("lumen")
    except PackageNotFoundError:
        installed = "not installed"
    return {
        "upstream_repository": LUMEN_REPOSITORY,
        "pinned_commit": LUMEN_BASELINE_COMMIT,
        "installed_version": installed,
        "python_version": platform.python_version(),
    }

"""Build and import an installed wheel outside the checkout; no upload or retained wheel."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory(prefix="opslens-wheel-") as temp:
        work = Path(temp)
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                str(ROOT),
                "--no-deps",
                "-w",
                str(work / "wheels"),
            ],
            check=True,
        )
        wheels = list((work / "wheels").glob("opslens-0.1.0-*.whl"))
        assert len(wheels) == 1
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", str(work / "venv")], check=True
        )
        python = work / "venv/bin/python"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "--python",
                str(python),
                "install",
                "--no-deps",
                str(wheels[0]),
            ],
            check=True,
            cwd=work,
        )
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
        subprocess.run(
            [
                str(python),
                "-I",
                "-c",
                "from importlib.metadata import version; import opslens; "
                'assert version("opslens") == opslens.__version__ == "0.1.0"; '
                'print("ISOLATED_WHEEL_IMPORT_VERSION_OK")',
            ],
            cwd=work,
            env=env,
            check=True,
        )
    print("PACKAGING_SMOKE_OK")


if __name__ == "__main__":
    main()

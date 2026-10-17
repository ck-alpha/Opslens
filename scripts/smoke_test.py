"""Credential-free runtime import checks; any failure exits nonzero."""

import importlib
import json
import sys

from opslens.runtime.lumen_baseline import get_lumen_runtime_info


def main() -> None:
    if sys.version_info < (3, 11):
        raise RuntimeError("Python >=3.11 required")
    print(f"[OK] Python {sys.version.split()[0]}")
    for name in ("opslens", "lumen", "lumen.ai"):
        importlib.import_module(name)
        print(f"[OK] {name} import")
    for module, symbol in (
        ("lumen.ai.coordinator", "Planner"),
        ("lumen.ai.agents.sql", "SQLAgent"),
        ("lumen.ai.agents.validation", "ValidationAgent"),
        ("lumen.ai.vector_store", "NumpyVectorStore"),
    ):
        getattr(importlib.import_module(module), symbol)
        print(f"[OK] {module}.{symbol}")
    info = get_lumen_runtime_info()
    if info["installed_version"] == "not installed":
        raise RuntimeError("Lumen distribution metadata missing")
    print(json.dumps(info, indent=2))
    print("[OK] baseline metadata")
    print("Runtime import smoke test passed")


if __name__ == "__main__":
    main()

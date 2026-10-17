"""Read-only readiness. Prints stable codes, never raw infrastructure exceptions."""

import json

from opslens.product.config import ProductConfig, readiness


def main():
    try:
        result = readiness(ProductConfig.from_env())
    except Exception:
        result = {"ready": False, "code": "INVALID_PRODUCT_CONFIGURATION"}
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(0 if result["ready"] else 1)


if __name__ == "__main__":
    main()

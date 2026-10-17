"""Start a single-instance local/staging console; no live model calls."""

import asyncio
import logging

import panel as pn
from alembic.config import Config

from alembic import command
from opslens.product.config import ProductConfig, build_product_service, ensure_demo, readiness
from opslens.ui.app import create_app


def main():
    logging.getLogger("sqlalchemy").setLevel(logging.CRITICAL)
    try:
        config = ProductConfig.from_env()
        ensure_demo(config, generate=True)
        command.upgrade(Config("alembic.ini"), "head")
        if not readiness(config)["ready"]:
            raise ValueError("NOT_READY")
        service = asyncio.run(build_product_service(config))
    except Exception:
        print("STARTUP_FAILED: check operator configuration, migration and demo integrity.")
        raise SystemExit(1) from None
    pn.extension()

    def application():
        try:
            return create_app(service, config)
        except Exception:
            return pn.pane.Str("PRODUCT_UNAVAILABLE — Contact the operator.")

    try:
        pn.serve(
            application,
            address=config.host,
            port=config.port,
            websocket_origin=list(config.origins),
            show=False,
            verbose=False,
        )
    finally:
        service.runtime.database.dispose()


if __name__ == "__main__":
    main()

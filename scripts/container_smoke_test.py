"""Build and verify a disposable Compose project; never print secrets or raw logs."""

import argparse
import os
import secrets
import subprocess
import time
from urllib.request import ProxyHandler, build_opener

CONTAINER_TOKENIZER_SMOKE = """
from unittest.mock import patch
with patch('requests.get', side_effect=AssertionError('TOKENIZER_NETWORK_ACCESS')):
    from lumen.ai.vector_store import NumpyVectorStore
    NumpyVectorStore()
print('CONTAINER_OFFLINE_TOKENIZER_OK')
"""

CONTAINER_JOURNEYS = """
import asyncio
from uuid import uuid4
from opslens.product.config import ProductConfig, build_product_service
from opslens.ui.app import create_app
async def check():
    service = await build_product_service(ProductConfig.from_env())
    try:
        create_app(service)
        assert (await service.clarify('8 月 GMV 是多少？')).action == 'ASK_USER'
        for recipe in service.list_demo_recipes():
            view = service.start(recipe.runtime_request, idempotency_key=uuid4().hex)
            expected = {'VERIFIED_ANALYSIS': 'VERIFIED', 'POLICY_DEMO': 'REJECTED',
                        'REVIEW_DEMO': 'NEEDS_REVIEW'}[recipe.category]
            assert view.status == expected, (recipe.recipe_id, view.reason_codes)
        print('CONTAINER_PRODUCT_JOURNEYS_OK')
    finally:
        service.runtime.database.dispose()
asyncio.run(check())
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cleanup-volumes",
        action="store_true",
        help="Only for this disposable smoke project's generated volumes",
    )
    args = parser.parse_args()
    project = "opslens-smoke-" + secrets.token_hex(4)
    password = secrets.token_urlsafe(32)
    env = {
        **os.environ,
        "POSTGRES_PASSWORD": password,
        "OPSLENS_HOST_PORT": "15006",
        "OPSLENS_ALLOWED_WS_ORIGINS": "localhost:15006,127.0.0.1:15006",
    }
    compose = ["docker", "compose", "--project-name", project]

    def run(*parts, capture=False):
        return subprocess.run(
            [*compose, *parts], env=env, check=True, text=True, capture_output=capture
        )

    try:
        run("config", "--quiet")
        run("build")
        run("up", "-d", "--wait", "--wait-timeout", "300")
        run("exec", "-T", "opslens", "python", "scripts/readiness_check.py")
        uid = run("exec", "-T", "opslens", "id", "-u", capture=True).stdout.strip()
        assert uid != "0" and uid.isdigit(), "CONTAINER_ROOT_USER"
        run(
            "exec",
            "-T",
            "opslens",
            "sh",
            "-c",
            "test ! -d /app/benchmarks && test ! -e /app/.env && ! command -v git",
        )
        # Data readiness can precede the Panel HTTP listener; bound this separate check.
        deadline = time.monotonic() + 60
        while True:
            try:
                with build_opener(ProxyHandler({})).open(
                    "http://127.0.0.1:15006/", timeout=5
                ) as response:
                    page = response.read().decode()
                    assert response.status == 200 and "OpsLens Console" in page
                    assert "PRODUCT_UNAVAILABLE" not in page
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("CONTAINER_HTTP_NOT_READY") from None
                time.sleep(0.5)
        run("exec", "-T", "opslens", "python", "-c", CONTAINER_TOKENIZER_SMOKE)
        run("exec", "-T", "opslens", "python", "-c", CONTAINER_JOURNEYS)
        image_id = run("images", "-q", "opslens", capture=True).stdout.strip()
        history = subprocess.run(
            ["docker", "history", "--no-trunc", image_id],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert password not in history, "CONTAINER_HISTORY_LEAK"
        logs = run("logs", "--no-color", "opslens", capture=True).stdout
        for forbidden in (
            password,
            "postgresql://",
            "postgresql+psycopg://",
            "DELETE FROM",
            "SELECT SUM",
        ):
            assert forbidden not in logs, "CONTAINER_LOG_LEAK"
        print("CONTAINER_SMOKE_OK: readiness, HTTP, non-root, truth isolation, safe logs")
    finally:
        # Never remove user deployment volumes. Only this randomly named smoke project.
        run("down", *(["-v"] if args.cleanup_volumes else []))


if __name__ == "__main__":
    main()

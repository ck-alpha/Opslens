"""Real Panel HTTP smoke, bounded startup and reliable subprocess cleanup."""

import os
import socket
import subprocess
import sys
import time
from urllib.request import ProxyHandler, build_opener


def main():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {
        **os.environ,
        "OPSLENS_UI_HOST": "127.0.0.1",
        "OPSLENS_UI_PORT": str(port),
        "OPSLENS_ALLOWED_WS_ORIGINS": f"127.0.0.1:{port},localhost:{port}",
    }
    started = time.monotonic()
    process = subprocess.Popen(
        [sys.executable, "scripts/serve_ui.py"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        while time.monotonic() - started < 90:
            if process.poll() is not None:
                raise RuntimeError("UI_SERVER_EXITED")
            try:
                with build_opener(ProxyHandler({})).open(
                    f"http://127.0.0.1:{port}/", timeout=3
                ) as response:
                    page = response.read().decode()
                    assert response.status == 200
                    assert "OpsLens Console" in page
                    assert "PRODUCT_UNAVAILABLE" not in page
                    assert "postgresql://" not in page and "Traceback" not in page
                    print(f"UI_HTTP_OK elapsed_seconds={time.monotonic() - started:.2f}")
                    return
            except OSError:
                time.sleep(0.25)
        raise RuntimeError("UI_STARTUP_TIMEOUT")
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Run every suite inside the test image.

The suites live in the `test` build stage and are deliberately absent from the
production image, so `docker compose exec web python tests/...` no longer works
against the running container. This is how they run now:

    docker compose -f docker-compose.test.yml run --rm -T web-test

A server is started here because the API suite talks to a real HTTP endpoint
rather than using Flask's test client. Each suite then runs as its own child
process, which also keeps the font engine's process-global state isolated.

Exits non-zero if any suite fails.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE_URL = os.environ.get("FBS_BASE_URL", "http://127.0.0.1:5000")
LOG_PATH = "/tmp/suites-server.log"

#: label, command, extra environment for that child only.
SUITES: list[tuple[str, list[str], dict[str, str]]] = [
    ("gate", ["python", "tools/gate_check.py"], {}),
    ("engine", ["python", "tests/engine_smoke.py"], {}),
    ("api", ["python", "tests/api_smoke.py"], {}),
    # The auth suite brings the app up in-process with its own temporary
    # database, so auth has to be enabled for that child.
    ("auth", ["python", "tests/auth_smoke.py"], {"ALLOW_AUTH": "true"}),
]


def wait_for_health(server: subprocess.Popen, timeout: float = 60.0) -> bool:
    """Poll /health until the server answers, or it dies."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if server.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(f"{BASE_URL}/health", timeout=2) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(1)
    return False


def main() -> int:
    log = open(LOG_PATH, "wb")
    server = subprocess.Popen(
        [
            "gunicorn",
            "--bind",
            "127.0.0.1:5000",
            "--workers",
            os.environ.get("GUNICORN_WORKERS", "2"),
            "--threads",
            "1",
            "--timeout",
            "180",
            "app:app",
        ],
        stdout=log,
        stderr=subprocess.STDOUT,
    )

    try:
        if not wait_for_health(server):
            log.flush()
            print("server did not become healthy; last output follows\n")
            with open(LOG_PATH, encoding="utf-8", errors="replace") as handle:
                print(handle.read()[-2000:])
            return 1

        failed = []
        for label, command, extra_env in SUITES:
            print(f"\n=== {label} ===", flush=True)
            if subprocess.run(command, env={**os.environ, **extra_env}).returncode != 0:
                failed.append(label)

        if failed:
            print(f"\nSUITES FAILED: {', '.join(failed)}")
            return 1
        print("\nALL SUITES PASSED")
        return 0
    finally:
        if server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()


if __name__ == "__main__":
    sys.exit(main())

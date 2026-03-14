"""Shared fixtures for Flint e2e tests."""

import os
import subprocess
import time

# Set test isolation env vars BEFORE importing flint
os.environ["FLINT_PORT"] = "9101"
os.environ["FLINT_DATA_DIR"] = "/microvms-test"
os.environ["FLINT_STATE_DIR"] = "/tmp/flint-test"

import pytest

from flint import Sandbox

_daemon_process = None


@pytest.fixture(scope="session", autouse=True)
def _ensure_daemon():
    global _daemon_process
    if Sandbox.is_daemon_running():
        yield
        return

    _daemon_process = subprocess.Popen(
        ["uv", "run", "flint", "start",
         "--port", "9101",
         "--data-dir", "/microvms-test",
         "--state-dir", "/tmp/flint-test"],
    )

    # Poll until healthy (golden snapshot can take a while)
    deadline = time.time() + 120
    while time.time() < deadline:
        if Sandbox.is_daemon_running():
            break
        time.sleep(1)
    else:
        _daemon_process.kill()
        pytest.exit("Daemon failed to start within 120s", returncode=1)

    yield

    _daemon_process.terminate()
    _daemon_process.wait(timeout=10)


@pytest.fixture
def sandbox():
    """Create a sandbox and kill it after the test."""
    sb = Sandbox()
    yield sb
    sb.kill()

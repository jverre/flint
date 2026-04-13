"""Shared fixtures for Flint e2e tests."""

import os
import subprocess
import tempfile
import time

import httpx

# Set test isolation env vars BEFORE importing flint.
TEST_PORT = "9101"
TEST_DATA_DIR = os.path.join(tempfile.gettempdir(), "flint-test-data")
TEST_STATE_DIR = os.path.join(tempfile.gettempdir(), "flint-test-state")

os.environ["FLINT_PORT"] = TEST_PORT
os.environ["FLINT_DATA_DIR"] = TEST_DATA_DIR
os.environ["FLINT_STATE_DIR"] = TEST_STATE_DIR

# On macOS, point VZ asset paths to the real install location so the test
# daemon can find the kernel + rootfs even though FLINT_DATA_DIR is overridden.
import platform as _platform

if _platform.system() == "Darwin":
    _real_vz_dir = os.path.join(
        os.path.expanduser("~"), "Library", "Application Support", "flint", "data", "vz"
    )
    os.environ.setdefault(
        "FLINT_VZ_KERNEL_PATH", os.path.join(_real_vz_dir, "vmlinux")
    )
    os.environ.setdefault(
        "FLINT_VZ_ROOTFS_PATH", os.path.join(_real_vz_dir, "rootfs.img")
    )

import pytest

from flint import Sandbox

_daemon_process = None


@pytest.fixture(scope="session", autouse=True)
def _ensure_daemon():
    global _daemon_process
    if Sandbox.is_daemon_running():
        yield
        return

    env = os.environ.copy()
    _daemon_process = subprocess.Popen(
        ["uv", "run", "flint", "start"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )

    # Poll until healthy (golden snapshot can take a while)
    deadline = time.time() + 120
    while time.time() < deadline:
        if Sandbox.is_daemon_running():
            break
        if _daemon_process.poll() is not None:
            output = ""
            if _daemon_process.stdout:
                output = _daemon_process.stdout.read()
            pytest.skip(f"Daemon failed to start on this host: {output.strip()}")
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
    resp = httpx.get(f"http://127.0.0.1:{TEST_PORT}/health", timeout=5.0)
    resp.raise_for_status()
    health = resp.json()
    if not health.get("golden_snapshot_ready", False):
        pytest.skip(
            f"Active backend {health.get('default_backend', 'unknown')} is not ready for sandbox creation on this host."
        )
    sb = Sandbox()
    yield sb
    sb.kill()


@pytest.fixture(scope="session")
def backend_health(_ensure_daemon):
    resp = httpx.get(f"http://127.0.0.1:{TEST_PORT}/health", timeout=5.0)
    resp.raise_for_status()
    return resp.json()


@pytest.fixture(scope="session")
def backend_kind(backend_health):
    return backend_health["default_backend"]

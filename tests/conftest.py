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
_daemon_log_path = os.path.join(tempfile.gettempdir(), "flint-test-daemon.log")

# Session-level flag: None = untested, True = works, str = error message
_sandbox_creation_result: str | bool | None = None


def _read_daemon_log() -> str:
    try:
        with open(_daemon_log_path) as f:
            return f.read()
    except OSError:
        return "(no daemon log available)"


@pytest.fixture(scope="session", autouse=True)
def _ensure_daemon():
    global _daemon_process
    if Sandbox.is_daemon_running():
        yield
        return

    env = os.environ.copy()
    print(f"Starting daemon: uv run flint start (port={env.get('FLINT_PORT')})")
    # Write daemon output to a file instead of a pipe to avoid pipe buffer
    # deadlock — the daemon prints extensively and nobody reads the pipe
    # during normal operation.
    log_fd = open(_daemon_log_path, "w")
    _daemon_process = subprocess.Popen(
        ["uv", "run", "flint", "start"],
        stdout=log_fd,
        stderr=subprocess.STDOUT,
        env=env,
    )

    # Poll until healthy (golden snapshot can take a while)
    deadline = time.time() + 120
    while time.time() < deadline:
        if Sandbox.is_daemon_running():
            break
        if _daemon_process.poll() is not None:
            log_fd.close()
            output = _read_daemon_log()
            print(f"=== Daemon stdout/stderr ===\n{output}\n=== End daemon output ===")
            pytest.exit(
                f"Daemon exited with code {_daemon_process.returncode}.\n{output.strip()}",
                returncode=1,
            )
        time.sleep(1)
    else:
        # Timed out — grab whatever output we have.
        _daemon_process.kill()
        _daemon_process.wait(timeout=5)
        log_fd.close()
        output = _read_daemon_log()
        print(f"=== Daemon stdout/stderr (timeout) ===\n{output}\n=== End daemon output ===")
        pytest.exit(
            f"Daemon failed to start within 120s.\n{output.strip()}",
            returncode=1,
        )

    yield

    _daemon_process.terminate()
    try:
        _daemon_process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        _daemon_process.kill()
        _daemon_process.wait(timeout=5)
    log_fd.close()


@pytest.fixture(scope="session")
def _sandbox_probe(_ensure_daemon):
    """Try to create a sandbox once per session. Cache the result."""
    global _sandbox_creation_result
    resp = httpx.get(f"http://127.0.0.1:{TEST_PORT}/health", timeout=5.0)
    resp.raise_for_status()
    health = resp.json()
    if not health.get("golden_snapshot_ready", False):
        _sandbox_creation_result = f"Backend {health.get('backend_kind', 'unknown')} not ready"
        return

    try:
        sb = Sandbox()
        sb.kill()
        _sandbox_creation_result = True
    except Exception as e:
        output = _read_daemon_log()
        print(f"=== Sandbox probe failed ===\n{e}\n=== Daemon log (last 3000 chars) ===\n{output[-3000:]}\n=== End ===")
        _sandbox_creation_result = str(e)


@pytest.fixture
def require_sandbox(_sandbox_probe):
    """Skip the test if sandbox creation is not available on this host."""
    if _sandbox_creation_result is not True:
        pytest.skip(f"Sandbox creation not available: {_sandbox_creation_result}")


@pytest.fixture
def sandbox(require_sandbox):
    """Create a sandbox and kill it after the test."""
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
    return backend_health["backend_kind"]

"""Shared fixtures for Flint e2e tests."""

import os
import subprocess
import tempfile
import time


# Set test isolation env vars BEFORE importing flint. FLINT_TEST_* override
# them, so two checkouts can run their suites on one machine side by side.
TEST_PORT = os.environ.get("FLINT_TEST_PORT", "9101")
TEST_DATA_DIR = os.environ.get("FLINT_TEST_DATA_DIR", os.path.join(tempfile.gettempdir(), "flint-test-data"))
TEST_STATE_DIR = os.environ.get("FLINT_TEST_STATE_DIR", os.path.join(tempfile.gettempdir(), "flint-test-state"))

os.environ["FLINT_PORT"] = TEST_PORT
os.environ["FLINT_DATA_DIR"] = TEST_DATA_DIR
os.environ["FLINT_STATE_DIR"] = TEST_STATE_DIR

# By default the suite talks to the daemon it starts the way a local user
# does, over its Unix socket. FLINT_URL points it at an already running
# endpoint instead, e.g. a cluster control plane (with FLINT_API_KEY).
FLINT_URL = os.environ.setdefault("FLINT_URL", f"unix://{TEST_STATE_DIR}/flint.sock").rstrip("/")
# The test daemon's TCP listener, which needs an API token.
TCP_URL = f"http://127.0.0.1:{TEST_PORT}"
AUTH = {"Authorization": f"Bearer {os.environ['FLINT_API_KEY']}"} if os.environ.get("FLINT_API_KEY") else {}

from flint._client import transport as _transport  # noqa: E402  (after the env above)

BASE_URL = _transport.http_base(FLINT_URL)
api = _transport.http_client(FLINT_URL, headers=AUTH, timeout=30.0)


def ws_connect(path: str, **kwargs):
    """A WebSocket to ``path`` on the endpoint under test."""
    return _transport.ws_connect(FLINT_URL, path, AUTH, **kwargs)

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


# A real OpenID provider for the SSO tests (test_sso.py), started before the
# daemon so its sso.json can point at it. None when it couldn't start.
MOCK_IDP: str | None = None
_mock_idp_server = None


def _start_mock_identity_provider() -> None:
    global MOCK_IDP, _mock_idp_server
    try:
        from oidc_provider_mock import run_server_in_thread
    except ImportError:
        return
    _mock_idp_server = run_server_in_thread()
    MOCK_IDP = f"http://localhost:{_mock_idp_server.__enter__().server_port}"
    import json
    os.makedirs(TEST_STATE_DIR, exist_ok=True)
    with open(os.path.join(TEST_STATE_DIR, "sso.json"), "w") as f:
        json.dump({
            "public_url": TCP_URL,
            "session_hours": 1,
            "providers": [{"id": "mock", "type": "oidc", "label": "Mock", "issuer": MOCK_IDP,
                           "client_id": "flint-tests", "client_secret": "not-secret"}],
            "rules": [
                {"email": "admin@flint.test", "kind": "admin"},
                {"group": "acme-support", "kind": "service", "tenants": ["acme"]},
            ],
        }, f)


@pytest.fixture(scope="session", autouse=True)
def _ensure_daemon(request):
    global _daemon_process
    cluster_dir = os.path.join(os.path.dirname(__file__), "cluster")
    if all(str(item.fspath).startswith(cluster_dir) for item in request.session.items):
        yield  # cluster tests run their own cluster
        return
    if Sandbox.is_daemon_running():
        yield
        return

    _start_mock_identity_provider()
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

    if _mock_idp_server is not None:
        _mock_idp_server.__exit__(None, None, None)
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
    resp = api.get(f"{BASE_URL}/health", timeout=5.0)
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
    """Skip the test if sandbox creation is not available on this host.

    In CI (GitHub Actions, on KVM hosts), or with FLINT_REQUIRE_VMS=1, that
    is a failure instead: a run whose VM tests all skipped proves nothing.
    """
    if _sandbox_creation_result is not True:
        why = f"Sandbox creation not available: {_sandbox_creation_result}"
        default = "1" if os.environ.get("GITHUB_ACTIONS") == "true" else ""
        if os.environ.get("FLINT_REQUIRE_VMS", default) == "1":
            pytest.fail(f"{why}\n--- daemon log tail ---\n{_read_daemon_log()[-4000:]}")
        pytest.skip(why)


@pytest.fixture
def sandbox(require_sandbox):
    """Create a sandbox and kill it after the test."""
    sb = Sandbox()
    yield sb
    sb.kill()


@pytest.fixture(scope="session")
def backend_health(_ensure_daemon):
    resp = api.get(f"{BASE_URL}/health", timeout=5.0)
    resp.raise_for_status()
    return resp.json()


@pytest.fixture(scope="session")
def daemon_only(backend_health):
    """Skip a test that checks something host-local (volumes, per-daemon config,
    jail directories) when the suite runs against a cluster control plane."""
    if backend_health.get("role") == "control-plane":
        pytest.skip("host-local feature: not served by the cluster control plane")


@pytest.fixture(scope="session")
def backend_kind(backend_health):
    return backend_health["backend_kind"]

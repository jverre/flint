"""Unit tests for the backend plugin registry and discovery."""

from __future__ import annotations

import pytest

from flint.core.backends import (
    BackendNotFound,
    BackendPlugin,
    available,
    default_for_host,
    get_backend,
    names,
    register,
    resolve_backend,
)
from flint.core.backends.registry import reset_for_tests


# The conftest for the rest of the suite spins up a daemon; these tests don't
# need it. Each test re-discovers the built-in plugins so ordering is stable.


@pytest.fixture(autouse=True)
def _fresh_registry():
    reset_for_tests()
    # Trigger built-in + entry-point discovery before each test runs so the
    # registry is back to a known state after ``reset_for_tests`` wiped it.
    names()
    yield
    reset_for_tests()
    names()


def test_builtins_are_discovered():
    discovered = names()
    # Firecracker and cloud-hypervisor are always importable on Linux; macos-vz
    # fails the pyobjc import on Linux, so we don't assert on it here.
    assert "firecracker" in discovered
    assert "cloud-hypervisor" in discovered


def test_get_backend_by_short_name():
    backend = get_backend("firecracker")
    assert backend.name == "firecracker"
    assert backend.kind == "linux-firecracker"


def test_get_backend_by_long_kind():
    backend = get_backend("linux-firecracker")
    assert backend.name == "firecracker"


def test_unknown_backend_raises_with_helpful_message():
    with pytest.raises(BackendNotFound) as excinfo:
        get_backend("does-not-exist")
    assert "does-not-exist" in str(excinfo.value)
    assert "available" in str(excinfo.value).lower()


def test_resolve_backend_honors_explicit_argument():
    backend = resolve_backend("cloud-hypervisor")
    assert backend.name == "cloud-hypervisor"


def test_resolve_backend_reads_env_var(monkeypatch):
    monkeypatch.setenv("FLINT_BACKEND", "firecracker")
    backend = resolve_backend()
    assert backend.name == "firecracker"


def test_available_returns_metadata():
    infos = available()
    by_name = {info.name: info for info in infos}
    assert "firecracker" in by_name
    fc = by_name["firecracker"]
    assert fc.kind == "linux-firecracker"
    assert "linux" in fc.supported_platforms
    # preflight_ok is bool (may be False on CI if binary is missing)
    assert isinstance(fc.preflight_ok, bool)
    # preflight_problems is a tuple of strings
    assert isinstance(fc.preflight_problems, tuple)


def test_default_for_host_returns_registered_name():
    import platform as _platform

    picked = default_for_host()
    if _platform.system().lower() == "linux":
        assert picked in {"firecracker", "cloud-hypervisor"}
    # On other systems, picked may be None or a match — the contract is just
    # "either None or a registered name".
    assert picked is None or picked in names()


def test_register_rejects_non_plugin():
    class NotAPlugin:
        pass

    with pytest.raises(TypeError):
        register(NotAPlugin)  # type: ignore[arg-type]


def test_register_rejects_duplicate_name():
    class MyBackend(BackendPlugin):
        name = "firecracker"  # collides with built-in
        kind = "my-kind"
        supported_platforms = ("linux",)

        def ensure_runtime_ready(self): pass
        def ensure_default_template(self): pass
        def start_pool(self): pass
        def stop_pool(self): pass
        def create(self, **kw): raise NotImplementedError
        def kill(self, entry): pass
        def pause(self, entry, state_store): pass
        def resume(self, row): raise NotImplementedError
        def proxy_guest_request(self, entry, method, path, body=None, timeout=65): raise NotImplementedError
        async def bridge_terminal(self, entry, websocket): raise NotImplementedError
        def check_entry_alive(self, entry): return False, None
        def recover_row(self, row): return "dead", None
        def build_template(self, name, dockerfile, rootfs_size_mb=500): raise NotImplementedError
        def delete_template_artifact(self, template_id, template=None): pass

    with pytest.raises(ValueError):
        register(MyBackend)


def test_plugin_metadata_is_set():
    for info in available():
        assert info.name, "plugin missing name"
        assert info.kind, "plugin missing kind"


def test_ch_backend_preflight_fails_when_binary_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("FLINT_CH_BINARY", str(tmp_path / "nope"))
    # Reimport the CH module so it picks up the overridden env var. The plugin
    # class reads CH_BINARY at preflight time from the config module, which is
    # imported eagerly — reloading it refreshes the constant.
    import importlib
    import flint.core.config as _cfg
    importlib.reload(_cfg)

    # PATH shadow so `shutil.which` can't find a system install.
    monkeypatch.setenv("PATH", str(tmp_path))

    from flint.core.backends.linux_cloud_hypervisor import LinuxCloudHypervisorBackend

    problems = LinuxCloudHypervisorBackend().preflight()
    assert any("cloud-hypervisor" in p.lower() for p in problems)


def test_fc_template_artifact_valid(tmp_path):
    from flint.core.backends.linux_firecracker import LinuxFirecrackerBackend

    backend = LinuxFirecrackerBackend()
    assert backend.template_artifact_valid(str(tmp_path)) is False
    for f in ("rootfs.ext4", "vmstate", "mem"):
        (tmp_path / f).write_bytes(b"")
    assert backend.template_artifact_valid(str(tmp_path)) is True


def test_ch_template_artifact_valid(tmp_path):
    from flint.core.backends.linux_cloud_hypervisor import LinuxCloudHypervisorBackend

    backend = LinuxCloudHypervisorBackend()
    assert backend.template_artifact_valid(str(tmp_path)) is False
    for f in ("rootfs.ext4", "config.json", "state.json", "memory-ranges"):
        (tmp_path / f).write_bytes(b"")
    assert backend.template_artifact_valid(str(tmp_path)) is True


def test_cli_backends_list_runs():
    """`flint backends list` exits 0 and prints plugin names."""
    from click.testing import CliRunner

    from flint.cli import cli

    result = CliRunner().invoke(cli, ["backends", "list"])
    assert result.exit_code == 0, result.output
    assert "firecracker" in result.output
    assert "cloud-hypervisor" in result.output


def test_cli_start_rejects_unknown_backend():
    from click.testing import CliRunner

    from flint.cli import cli

    result = CliRunner().invoke(cli, ["start", "--backend", "does-not-exist"])
    assert result.exit_code != 0
    assert "does-not-exist" in result.output


def test_ch_backend_metadata():
    from flint.core.backends.linux_cloud_hypervisor import LinuxCloudHypervisorBackend

    assert LinuxCloudHypervisorBackend.name == "cloud-hypervisor"
    assert LinuxCloudHypervisorBackend.kind == "linux-cloud-hypervisor"
    assert LinuxCloudHypervisorBackend.display_name
    assert "linux" in LinuxCloudHypervisorBackend.supported_platforms


def test_ch_lifecycle_methods_raise_not_implemented():
    """CH is a skeleton today — lifecycle calls must raise a clear error,
    not silently return garbage. Lift these asserts when the boot pipeline lands."""
    from flint.core.backends.linux_cloud_hypervisor import LinuxCloudHypervisorBackend

    backend = LinuxCloudHypervisorBackend()

    with pytest.raises(NotImplementedError):
        backend.create(
            template_id="default",
            allow_internet_access=False,
            use_pool=False,
            use_pyroute2=False,
        )
    with pytest.raises(NotImplementedError):
        backend.pause(entry=None, state_store=None)
    with pytest.raises(NotImplementedError):
        backend.resume({})
    with pytest.raises(NotImplementedError):
        backend.build_template("x", "FROM alpine")


def test_ch_recover_row_returns_dead():
    """Recovery for a WIP backend should never claim a stale row is alive."""
    from flint.core.backends.linux_cloud_hypervisor import LinuxCloudHypervisorBackend

    state, entry = LinuxCloudHypervisorBackend().recover_row({"vm_id": "x", "pid": 1})
    assert state == "dead"
    assert entry is None


def test_ch_kind_registered_for_state_store_lookup():
    """State-store rows persist `backend_kind="linux-cloud-hypervisor"`;
    `get_backend` must resolve that long form too."""
    backend = get_backend("linux-cloud-hypervisor")
    assert backend.name == "cloud-hypervisor"
    assert backend.kind == "linux-cloud-hypervisor"


def test_ch_api_client_builds_http_request(monkeypatch):
    """Smoke-test the HTTP-over-UDS protocol wrapper: verify it sends a
    well-formed request and decodes the status code correctly. We replace the
    socket with an in-memory fake so the test needs no real CH binary."""
    import io
    import flint.core._cloud_hypervisor as ch

    sent: dict[str, bytes] = {}

    class FakeSocket:
        def __init__(self, *a, **kw):
            self._buf = io.BytesIO()

        def connect(self, path):
            sent["path"] = path

        def sendall(self, data):
            sent["data"] = data

        def recv(self, n):
            if self._buf.tell() == 0:
                self._buf.write(b"HTTP/1.1 204 No Content\r\n\r\n")
                self._buf.seek(0)
            return self._buf.read(n)

        def settimeout(self, t): pass
        def close(self): pass

    monkeypatch.setattr(ch.socket, "socket", lambda *a, **kw: FakeSocket())

    status, body = ch.ch_put("/fake/sock", "/api/v1/vm.boot")
    assert status == 204
    assert ch.ch_status_ok(status)
    assert sent["path"] == "/fake/sock"
    assert sent["data"].startswith(b"PUT /api/v1/vm.boot HTTP/1.1\r\n")
    assert b"Host: localhost" in sent["data"]


def test_ch_api_client_serializes_json_body(monkeypatch):
    import flint.core._cloud_hypervisor as ch

    sent: dict[str, bytes] = {}

    class FakeSocket:
        def __init__(self, *a, **kw): pass
        def connect(self, path): pass
        def sendall(self, data): sent["data"] = data
        def recv(self, n):
            return b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"
        def settimeout(self, t): pass
        def close(self): pass

    monkeypatch.setattr(ch.socket, "socket", lambda *a, **kw: FakeSocket())

    ch.ch_put("/x", "/api/v1/vm.snapshot", {"destination_url": "file:///tmp/snap"})
    data = sent["data"]
    assert b"Content-Type: application/json" in data
    assert b'"destination_url": "file:///tmp/snap"' in data


def test_ch_api_client_rejects_error_status(monkeypatch):
    """Helper wrappers (vm_create etc.) must raise on a non-2xx response."""
    import flint.core._cloud_hypervisor as ch

    class FakeSocket:
        def __init__(self, *a, **kw): pass
        def connect(self, p): pass
        def sendall(self, d): pass
        def recv(self, n):
            return b"HTTP/1.1 400 Bad Request\r\nContent-Length: 5\r\n\r\nnope!"
        def settimeout(self, t): pass
        def close(self): pass

    monkeypatch.setattr(ch.socket, "socket", lambda *a, **kw: FakeSocket())

    with pytest.raises(RuntimeError, match="vm.boot"):
        ch.vm_boot("/x")
    with pytest.raises(RuntimeError, match="vm.create"):
        ch.vm_create("/x", {"cpus": {"boot_vcpus": 1}})


def test_ch_api_client_exposes_full_verb_surface():
    """The protocol surface the plugin relies on must be importable."""
    import flint.core._cloud_hypervisor as ch

    for name in (
        "ch_put", "ch_post", "ch_get", "ch_status_ok",
        "wait_for_api_socket",
        "vm_create", "vm_boot", "vm_pause", "vm_resume",
        "vm_snapshot", "vm_restore", "vmm_shutdown",
    ):
        assert callable(getattr(ch, name)), name

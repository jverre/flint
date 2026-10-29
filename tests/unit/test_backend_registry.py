"""Unit tests for the backend plugin registry and discovery."""

from __future__ import annotations

import sys

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
        def pause(self, entry): return ""
        def resume(self, row): raise NotImplementedError
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


@pytest.mark.skipif(sys.platform != "linux", reason="CH backend preflight is only reachable on Linux")
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
    """The CH default template is fresh-boot from rootfs.ext4 — no pre-baked
    snapshot files are required for the artifact to count as valid."""
    from flint.core.backends.linux_cloud_hypervisor import LinuxCloudHypervisorBackend

    backend = LinuxCloudHypervisorBackend()
    assert backend.template_artifact_valid(str(tmp_path)) is False
    (tmp_path / "rootfs.ext4").write_bytes(b"")
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


def test_ch_create_requires_golden_rootfs(monkeypatch, tmp_path):
    """Calling create() when the CH golden rootfs is missing must raise a
    clear RuntimeError instead of crashing deeper in the boot pipeline."""
    import flint.core.backends.linux_cloud_hypervisor as _mod
    import flint.core.config as _cfg

    monkeypatch.setattr(_cfg, "CH_GOLDEN_DIR", str(tmp_path / "absent"))

    backend = _mod.LinuxCloudHypervisorBackend()
    with pytest.raises(RuntimeError, match="golden"):
        backend.create(
            template_id="default",
            allow_internet_access=False,
            use_pool=False,
            use_pyroute2=False,
        )


def test_ch_recover_row_returns_dead_for_missing_pid():
    """Recovery of a row whose PID no longer exists must report 'dead'."""
    from flint.core.backends.linux_cloud_hypervisor import LinuxCloudHypervisorBackend

    # PID 999999 is well above typical /proc/sys/kernel/pid_max on Linux CI
    # runners and is extremely unlikely to be assigned.
    state, entry = LinuxCloudHypervisorBackend().recover_row(
        {"vm_id": "x", "pid": 999999, "vm_dir": "", "state": "Running"}
    )
    assert state == "dead"
    assert entry is None


def test_ch_recover_row_returns_paused_for_paused_row():
    """Paused rows never need a live PID probe — they come back as paused."""
    from flint.core.backends.linux_cloud_hypervisor import LinuxCloudHypervisorBackend

    state, entry = LinuxCloudHypervisorBackend().recover_row(
        {"vm_id": "x", "pid": 0, "vm_dir": "", "state": "Paused"}
    )
    assert state == "paused"
    assert entry is None


def test_ch_backend_metadata_ns_and_tap_names():
    """ns/tap helpers must use the ``ch-`` prefix so FC and CH can coexist
    without colliding on the same host."""
    from flint.core.vmm.cloud_hypervisor.boot import ns_name_for, tap_for_ns

    ns = ns_name_for("abcdef12-0000-0000-0000-000000000000")
    tap = tap_for_ns(ns)
    assert ns.startswith("ch-")
    assert tap.startswith("chtap-")


def test_ch_strip_ns_prefix_handles_fc_and_ch():
    """The shared netns helpers key veth pair names by the vm-id suffix; the
    split must work for both backends' prefixes."""
    from flint.core.net.netns import _strip_ns_prefix

    assert _strip_ns_prefix("fc-abcd1234") == "abcd1234"
    assert _strip_ns_prefix("ch-abcd1234") == "abcd1234"


def test_ch_kind_registered_for_state_store_lookup():
    """State-store rows persist `backend_kind="linux-cloud-hypervisor"`;
    `get_backend` must resolve that long form too."""
    backend = get_backend("linux-cloud-hypervisor")
    assert backend.name == "cloud-hypervisor"
    assert backend.kind == "linux-cloud-hypervisor"


class _FakeChApi:
    """A Unix-socket HTTP server that, like cloud-hypervisor, answers but keeps the connection open."""

    def __init__(self, path: str, response: bytes) -> None:
        import socket
        import threading

        self.requests: list[bytes] = []
        self.fds: list[int] = []
        self._response = response
        self._server = socket.socket(socket.AF_UNIX)
        self._server.bind(path)
        self._server.listen(4)
        self._conns: list = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        import socket

        while True:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            self._conns.append(conn)
            data, fds, _, _ = socket.recv_fds(conn, 65536, 4)
            self.requests.append(data)
            self.fds.extend(fds)
            conn.sendall(self._response)  # and leave the connection open

    def close(self) -> None:
        import os

        for fd in self.fds:
            os.close(fd)
        for conn in self._conns:
            conn.close()
        self._server.close()


@pytest.fixture
def fake_ch_api(tmp_path):
    servers = []

    def start(response: bytes) -> str:
        path = str(tmp_path / f"ch{len(servers)}.sock")
        servers.append(_FakeChApi(path, response))
        return path

    start.servers = servers
    yield start
    for server in servers:
        server.close()


def test_ch_api_client_returns_as_soon_as_response_is_complete(fake_ch_api):
    """CH keeps the connection open; the client must not wait for it to close."""
    import time

    import flint.core.vmm.cloud_hypervisor.api as ch

    sock = fake_ch_api(b"HTTP/1.1 204 No Content\r\n\r\n")
    t0 = time.monotonic()
    status, _ = ch.ch_put(sock, "/api/v1/vm.boot")
    assert status == 204 and ch.ch_status_ok(status)
    assert time.monotonic() - t0 < 1.0

    sock = fake_ch_api(b'HTTP/1.1 200 OK\r\nContent-Length: 13\r\n\r\n{"state":"x"}')
    status, body = ch.ch_put(sock, "/api/v1/vm.snapshot", {"destination_url": "file:///snap"})
    assert (status, body) == (200, b'{"state":"x"}')
    request = fake_ch_api.servers[-1].requests[0]
    assert request.startswith(b"PUT /api/v1/vm.snapshot HTTP/1.1\r\n")
    assert b'"destination_url": "file:///snap"' in request


def test_ch_api_client_rejects_error_status(fake_ch_api):
    """Helper wrappers (vm_create etc.) must raise on a non-2xx response."""
    import flint.core.vmm.cloud_hypervisor.api as ch

    sock = fake_ch_api(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 5\r\n\r\nnope!")
    with pytest.raises(RuntimeError, match="vm.boot"):
        ch.vm_boot(sock)
    with pytest.raises(RuntimeError, match="vm.create"):
        ch.vm_create(sock, {"cpus": {"boot_vcpus": 1}})


def test_ch_api_client_passes_tap_fd(fake_ch_api, tmp_path):
    """The TAP is handed to the jailed VMM as an fd alongside the request."""
    import os

    import flint.core.vmm.cloud_hypervisor.api as ch

    sock = fake_ch_api(b"HTTP/1.1 204 No Content\r\n\r\n")
    (tmp_path / "tap").write_text("")
    fd = os.open(tmp_path / "tap", os.O_RDONLY)
    try:
        ch.vm_restore(sock, "file:///pause", {"net0": fd})
    finally:
        os.close(fd)
    server = fake_ch_api.servers[-1]
    assert len(server.fds) == 1
    assert os.fstat(server.fds[0]).st_ino == os.stat(tmp_path / "tap").st_ino
    assert b'"net_fds": [{"id": "net0", "num_fds": 1' in server.requests[0]


def test_ch_api_client_exposes_full_verb_surface():
    """The protocol surface the plugin relies on must be importable."""
    import flint.core.vmm.cloud_hypervisor.api as ch

    for name in (
        "ch_put", "ch_post", "ch_get", "ch_status_ok",
        "wait_for_api_socket",
        "vm_create", "vm_boot", "vm_pause", "vm_resume",
        "vm_snapshot", "vm_restore", "vmm_shutdown",
    ):
        assert callable(getattr(ch, name)), name

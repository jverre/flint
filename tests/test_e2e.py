"""End-to-end tests for Flint sandboxes.

Requires a running daemon (`flint start`).
"""

import json
import os
import platform
import threading
import time

import httpx
import pytest

from .conftest import AUTH, BASE_URL, TEST_STATE_DIR, api
from flint import Sandbox, Template


# ── Lifecycle ────────────────────────────────────────────────────────────────


def test_create_and_kill(sandbox):
    assert sandbox.id
    assert sandbox.is_running()
    sandbox.kill()
    # Give daemon a moment to update state
    time.sleep(0.3)
    assert not sandbox.is_running()


def test_kill_paused_sandbox(sandbox):
    """Killing a paused sandbox deletes it; it can't be resumed afterwards."""
    sandbox.pause()
    sandbox.kill()
    assert sandbox.state == "Dead"
    with pytest.raises(RuntimeError):
        sandbox.resume()


def test_sandbox_properties(sandbox):
    assert sandbox.id
    assert sandbox.pid > 0
    assert sandbox.created_at > 0
    assert sandbox.ready_time_ms is not None
    assert sandbox.ready_time_ms > 0
    assert isinstance(sandbox.timings, dict)


def test_list_includes_sandbox(sandbox):
    ids = [s.id for s in Sandbox.list()]
    assert sandbox.id in ids


def test_connect_to_existing(sandbox):
    reconnected = Sandbox.connect(sandbox.id)
    assert reconnected.id == sandbox.id
    assert reconnected.is_running()


def test_health_reports_backend(backend_kind):
    resp = api.get(f"{BASE_URL}/health", timeout=5.0)
    resp.raise_for_status()
    assert resp.json()["backend_kind"] == backend_kind


def test_web_ui_served_for_deep_links():
    """The admin UI shell loads at /ui and for client-side routes (or explains how to build it)."""
    resp = api.get(f"{BASE_URL}/ui/vms/some-vm", timeout=5.0)
    assert resp.headers["content-type"].startswith("text/html"), resp.text[:200]
    if resp.status_code == 200:
        assert '<div id="root">' in resp.text
    else:
        assert resp.status_code == 503 and "build-ui.sh" in resp.text, resp.text[:300]


# ── Command execution ────────────────────────────────────────────────────────


def test_run_echo(sandbox):
    result = sandbox.commands.run("echo hello")
    assert result.exit_code == 0
    assert "hello" in result.stdout


def test_run_exit_code(sandbox):
    result = sandbox.commands.run("exit 42")
    assert result.exit_code == 42


def test_run_multiline_output(sandbox):
    result = sandbox.commands.run("echo line1 && echo line2 && echo line3")
    assert result.exit_code == 0
    lines = result.stdout.strip().splitlines()
    assert lines == ["line1", "line2", "line3"]


def test_run_env_vars(sandbox):
    result = sandbox.commands.run("export FOO=bar && echo $FOO")
    assert result.exit_code == 0
    assert "bar" in result.stdout


def test_run_working_directory(sandbox):
    result = sandbox.commands.run("cd /tmp && pwd")
    assert result.exit_code == 0
    assert "/tmp" in result.stdout


def test_run_sequential_commands(sandbox):
    """Multiple commands on the same sandbox share filesystem state."""
    sandbox.commands.run("touch /tmp/flint_test_file")
    result = sandbox.commands.run("ls /tmp/flint_test_file")
    assert result.exit_code == 0
    assert "flint_test_file" in result.stdout


def test_run_on_stdout_callback(sandbox):
    lines = []
    result = sandbox.commands.run("echo alpha && echo beta", on_stdout=lines.append)
    assert result.exit_code == 0
    assert any("alpha" in l for l in lines)
    assert any("beta" in l for l in lines)


# ── PTY ──────────────────────────────────────────────────────────────────────


def test_pty_session(sandbox):
    """Open a PTY, send a command, and verify output is received."""
    output = []
    done = threading.Event()

    def on_data(data: bytes):
        output.append(data)
        if b"PTY_OK" in b"".join(output):
            done.set()

    pty = sandbox.pty.create(on_data=on_data)
    try:
        pty.send_input('echo "PTY_OK"\n')
        assert done.wait(timeout=5), "Did not receive PTY output in time"
        full = b"".join(output).decode(errors="replace")
        assert "PTY_OK" in full
    finally:
        pty.kill()


# ── Concurrent execution ─────────────────────────────────────────────────────


def test_concurrent_commands(sandbox):
    """Run 5 commands in parallel on the same sandbox."""
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(sandbox.commands.run, f"echo {i}"): i for i in range(5)}
        results = {}
        for f in concurrent.futures.as_completed(futures):
            i = futures[f]
            results[i] = f.result()

    for i in range(5):
        assert results[i].exit_code == 0
        assert str(i) in results[i].stdout


def test_concurrent_commands_isolation(sandbox):
    """Many concurrent commands run side by side, with independent stdout/stderr."""
    import concurrent.futures

    def slow_cmd(n):
        # Use sleep 2 (busybox doesn't support fractional seconds)
        return sandbox.commands.run(f"sleep 2 && echo result-{n}")

    t0 = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=24) as pool:
        futures = [pool.submit(slow_cmd, i) for i in range(24)]
        results = [f.result() for f in futures]
    elapsed = time.monotonic() - t0
    assert elapsed < 5, f"24 two-second commands took {elapsed:.1f}s: they were queued, not run together"

    for i, r in enumerate(results):
        assert r.exit_code == 0
        assert f"result-{i}" in r.stdout


def test_stderr_separate(sandbox):
    """Verify stderr is captured separately from stdout."""
    result = sandbox.commands.run("echo out && echo err >&2")
    assert result.exit_code == 0
    assert "out" in result.stdout
    assert "err" in result.stderr


def test_pause_and_resume(sandbox):
    sandbox.commands.run("echo persisted > /tmp/persist.txt")
    sandbox.pause()
    listed = {vm["vm_id"]: vm["state"] for vm in api.get(f"{BASE_URL}/vms", timeout=10.0).json()["vms"]}
    assert listed.get(sandbox.id) == "Paused", f"paused VM missing from GET /vms: {listed}"
    sandbox.resume()
    result = sandbox.commands.run("cat /tmp/persist.txt")
    assert result.exit_code == 0
    assert "persisted" in result.stdout


# ── Filesystem operations ────────────────────────────────────────────────────


def test_write_and_read_file(sandbox):
    """Write a file via SDK, read it back."""
    sandbox.write_file("/tmp/test.txt", "hello flintd")
    data = sandbox.read_file("/tmp/test.txt")
    assert data == b"hello flintd"


def test_list_files(sandbox):
    """List files in a directory."""
    sandbox.commands.run("touch /tmp/a.txt /tmp/b.txt")
    entries = sandbox.list_files("/tmp")
    names = [e["name"] for e in entries]
    assert "a.txt" in names
    assert "b.txt" in names


# ── run_code ─────────────────────────────────────────────────────────────────


def test_run_code_python(sandbox):
    """Test run_code with Python — returns error if runtime unavailable."""
    result = sandbox.run_code("print(1 + 2)", runtime="python3")
    if result.exit_code == 0:
        assert "3" in result.stdout
    else:
        # python3 not installed in Alpine base image — should get 127 (not found)
        assert result.exit_code == 127


# ── Multiple sandboxes ───────────────────────────────────────────────────────


def test_multiple_sandboxes(require_sandbox):
    """Create two sandboxes and verify they are isolated."""
    sb1 = Sandbox()
    sb2 = Sandbox()
    try:
        assert sb1.id != sb2.id

        sb1.commands.run("touch /tmp/only_in_sb1")
        result = sb2.commands.run("ls /tmp/only_in_sb1 2>&1")
        assert result.exit_code != 0  # File should not exist in sb2
    finally:
        sb1.kill()
        sb2.kill()


# ── Storage backend ─────────────────────────────────────────────────────────


def test_health_reports_storage_backend():
    """Health endpoint exposes the active storage backend and its health."""
    resp = api.get(f"{BASE_URL}/health", timeout=5.0)
    resp.raise_for_status()
    data = resp.json()
    # storage_backend should always be present (defaults to "local").
    assert "storage_backend" in data, f"health response missing storage_backend: {data}"
    assert data["storage_backend"] in ("local", "s3_files", "r2")
    assert "storage_healthy" in data
    assert data["storage_healthy"] is True


def test_sandbox_reports_storage_backend(sandbox):
    """Sandbox object exposes the configured storage backend and workspace dir."""
    assert sandbox.storage_backend in ("local", "s3_files", "r2")
    assert sandbox.workspace_dir  # non-empty string


def test_workspace_file_ops(sandbox):
    """Write a file to workspace, read it back, verify it appears in listing."""
    ws = sandbox.workspace_dir
    sandbox.write_file(f"{ws}/storage-test.txt", "hello storage")
    data = sandbox.read_file(f"{ws}/storage-test.txt")
    assert data == b"hello storage"

    entries = sandbox.list_files(ws)
    names = [e["name"] for e in entries]
    assert "storage-test.txt" in names


def test_workspace_isolation(require_sandbox):
    """Two sandboxes cannot see each other's workspace files."""
    sb1 = Sandbox()
    sb2 = Sandbox()
    try:
        ws = sb1.workspace_dir
        sb1.write_file(f"{ws}/private.txt", "sb1-only")

        # sb2 should not see sb1's file.
        result = sb2.commands.run(f"cat {ws}/private.txt 2>&1")
        assert result.exit_code != 0, "sb2 should not read sb1's workspace file"
    finally:
        sb1.kill()
        sb2.kill()


def test_workspace_persists_across_pause_resume(sandbox):
    """Files written to workspace survive pause/resume."""
    ws = sandbox.workspace_dir
    sandbox.write_file(f"{ws}/persist.txt", "survives")
    sandbox.pause()
    sandbox.resume()
    data = sandbox.read_file(f"{ws}/persist.txt")
    assert data == b"survives"


# Public ECR mirrors Docker Hub's official images without its anonymous pull limits.
_PYTHON_IMAGE = "public.ecr.aws/docker/library/python:3.12-alpine"


def _import(name: str, image: str, **kwargs) -> Template:
    """Import, waiting out a previous attempt's worker still releasing its resources."""
    deadline = time.monotonic() + 30
    while True:
        try:
            return Template.import_image(name, image, timeout=1200, **kwargs)
        except httpx.HTTPStatusError as exc:
            assert exc.response.status_code in (409, 429) and time.monotonic() < deadline, exc.response.text
            time.sleep(.05)


def _linux_imports_only(backend_kind):
    if backend_kind not in ("linux-firecracker", "linux-cloud-hypervisor"):
        pytest.skip("Template imports require a Linux hypervisor")


def test_template_import_and_boot(backend_kind, require_sandbox, daemon_only):
    _linux_imports_only(backend_kind)
    import uuid
    name = "import-" + uuid.uuid4().hex[:8]
    try:
        # A failed import says why, and the name can be imported again.
        with pytest.raises(RuntimeError, match="doesn't exist or needs registry credentials"):
            _import(name, "public.ecr.aws/docker/library/flint-no-such-image:1")
        template = _import(name, _PYTHON_IMAGE, rootfs_size_mb=1024)
        assert template.status == "ready" and template.digest.startswith("sha256:"), template
        assert template.image.endswith("library/python:3.12-alpine") and not template.tenant, template

        def check_boots():
            sandbox = Sandbox(template_id=template.template_id)
            try:
                r = sandbox.commands.run("python3 -c 'import sys; print(sys.version_info[:2])'; echo $PYTHON_VERSION")
                # The image's own binaries, with its ENV (as `docker run` gives them).
                assert r.exit_code == 0 and "(3, 12)" in r.stdout and "\n3.12." in r.stdout, r
            finally:
                sandbox.kill()

        check_boots()
        # A failed re-import (the image doesn't fit) reports it and keeps the
        # published template bootable, at the same digest.
        with pytest.raises(RuntimeError, match="raise rootfs_size_mb"):
            _import(name, _PYTHON_IMAGE, rootfs_size_mb=64)
        assert Template.get(template.template_id).digest == template.digest
        check_boots()
    finally:
        api.delete(f"{BASE_URL}/templates/{name}")


def test_template_from_a_distroless_image_boots(backend_kind, require_sandbox, daemon_only):
    """An image with no shell or tools still boots, gets its network and runs commands."""
    _linux_imports_only(backend_kind)
    import uuid
    name = "distroless-" + uuid.uuid4().hex[:8]
    try:
        template = _import(name, "gcr.io/distroless/static-debian12:latest", rootfs_size_mb=256)
        sandbox = Sandbox(template_id=template.template_id)
        try:
            r = sandbox.commands.run("echo shell-ok && ip -4 addr show eth0 && test -f /etc/os-release")
            assert r.exit_code == 0 and "shell-ok" in r.stdout and "172.16.0.2" in r.stdout, r
        finally:
            sandbox.kill()
    finally:
        api.delete(f"{BASE_URL}/templates/{name}")


def test_localhost_works_in_the_sandbox(sandbox):
    """Programs can serve and connect on 127.0.0.1 (the guest agent listens on all addresses)."""
    result = sandbox.commands.run("wget -q -T 5 -O - http://127.0.0.1:5000/health")
    assert result.exit_code == 0, f"loopback unreachable: {result.stdout}{result.stderr}"


# ── Network policy ─────────────────────────────────────────────────────────


def test_sandbox_cannot_reach_other_sandboxes_on_bridge(sandbox):
    """No other sandbox's network namespace answers on the shared bridge.

    Another sandbox and the daemon's pre-built namespaces are on the bridge
    during the sweep. Only the sandbox's own namespace may reply, which also
    proves the probe itself works.
    """
    from flint.core.config import VETH_SUBNET

    other = Sandbox()
    try:
        sweep = sandbox.commands.run(
            f"for i in $(seq 2 254); do (ping -c1 -W1 {VETH_SUBNET}.$i >/dev/null 2>&1 && echo {VETH_SUBNET}.$i) & done; wait",
            timeout=30,
        )
        responders = sweep.stdout.split()
        assert len(responders) == 1, f"bridge addresses answering from inside a sandbox: {responders}"
    finally:
        other.kill()


def test_sandbox_reaches_internet(sandbox):
    """A sandbox with internet access can resolve names and open TCP connections out."""
    dns = sandbox.commands.run("nslookup example.com 8.8.8.8")
    assert dns.exit_code == 0, dns
    tcp = sandbox.commands.run("nc -w5 1.1.1.1 80 </dev/null")
    assert tcp.exit_code == 0, tcp


def test_sandbox_cannot_reach_host_services(sandbox):
    """A service listening on every host address is unreachable from inside a sandbox."""
    import socket

    from flint.core.config import BRIDGE_IP

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.connect(("8.8.8.8", 53))
    host_ip = probe.getsockname()[0]
    probe.close()

    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", 0))
    server.listen(16)
    port = server.getsockname()[1]
    try:
        for ip in (BRIDGE_IP, host_ip):
            result = sandbox.commands.run(f"nc -w3 {ip} {port} </dev/null")
            assert result.exit_code != 0, f"sandbox reached host service at {ip}:{port}"
    finally:
        server.close()


def _state_files_text() -> str:
    """Everything the test daemon keeps on disk in its database, as text."""
    out = []
    for name in ("flint.db", "flint.db-wal", "state.json"):
        try:
            with open(os.path.join(TEST_STATE_DIR, name), "rb") as f:
                out.append(f.read().decode("latin-1"))
        except OSError:
            pass
    return "\n".join(out)


def test_network_policy_round_trip(sandbox, backend_health, daemon_only):
    """Credentials are write-only: read back redacted, kept when sent back
    redacted (as the admin UI does), and never stored in plaintext."""
    if backend_health.get("network_policy") is False:
        pytest.skip(f"{backend_health['backend_kind']} can't apply a network policy")
    policy = {
        "allow": {
            "api.example.com": [
                {"transform": [{"headers": {"Authorization": "Bearer test-token-0451"}}]}
            ],
            "*.internal.io": [
                {"transform": [{"headers": {"X-Api-Key": "key-123"}}]}
            ],
        }
    }
    sandbox.update_network_policy(policy)
    got = sandbox.get_network_policy()
    assert set(got["allow"]) == {"api.example.com", "*.internal.io"}, got
    assert got["allow"]["api.example.com"][0]["transform"][0]["headers"]["Authorization"] == "Bear***0451"
    assert got["allow"]["*.internal.io"][0]["transform"][0]["headers"]["X-Api-Key"] == "***"
    for where, text in (("GET /vms/{id}", json.dumps(sandbox._fetch())), ("GET /vms", json.dumps(api.get(f"{BASE_URL}/vms").json())),
                        ("the daemon's state files", _state_files_text())):
        assert "test-token-0451" not in text and "key-123" not in text, f"credential visible in {where}"

    # A save replaces the whole policy: a removed domain is gone, a changed value is new.
    sandbox.update_network_policy({"allow": {"api.example.com": [{"transform": [{"headers": {"Authorization": "Bearer rotated-0452"}}]}]}})
    got = sandbox.get_network_policy()
    assert list(got["allow"]) == ["api.example.com"], got
    assert got["allow"]["api.example.com"][0]["transform"][0]["headers"]["Authorization"] == "Bear***0452"


def test_network_policy_refused_where_unsupported(sandbox, backend_health):
    """A backend that can't apply a policy refuses it, and the VM keeps the one it had."""
    if backend_health.get("network_policy") is not False:
        pytest.skip(f"{backend_health['backend_kind']} applies network policies")
    with pytest.raises(httpx.HTTPStatusError) as e:
        sandbox.update_network_policy({"allow": {"api.example.com": [{"transform": [{"headers": {"X-Key": "k"}}]}]}})
    assert e.value.response.status_code == 501, e.value.response.text
    assert sandbox.get_network_policy() is None


def test_network_policy_injects_credentials_over_https(require_sandbox):
    """The proxy injects the policy's header into the guest's HTTPS requests, replacing any the guest sent.

    Uses a public echo service: injection is TLS-only and keyed on the SNI, so
    it has to be a real domain with a real certificate.
    """
    if platform.system() != "Linux":
        pytest.skip("Network policy injection requires Linux (iptables)")
    sb = Sandbox(network_policy={
        "allow": {"postman-echo.com": [{"transform": [{"headers": {"X-Flint-Auth": "secret-12345"}}]}]}
    })
    try:
        result = sb.commands.run(
            'wget -q -O - --header="X-Flint-Auth: guest-value" https://postman-echo.com/headers', timeout=30
        )
        assert result.exit_code == 0, f"HTTPS through the proxy failed: {result.stdout}{result.stderr}"
        headers = {k.lower(): v for k, v in json.loads(result.stdout)["headers"].items()}
        assert headers.get("x-flint-auth") == "secret-12345", headers

        # Read the policy (redacted), add a header and write it back: the
        # credential the client never saw is still the one injected.
        policy = sb.get_network_policy()
        assert "secret-12345" not in json.dumps(policy), policy
        policy["allow"]["postman-echo.com"][0]["transform"][0]["headers"]["X-Flint-Extra"] = "extra"
        sb.update_network_policy(policy)
        result = sb.commands.run("wget -q -O - https://postman-echo.com/headers", timeout=30)
        assert result.exit_code == 0, f"HTTPS through the proxy failed: {result.stdout}{result.stderr}"
        headers = {k.lower(): v for k, v in json.loads(result.stdout)["headers"].items()}
        assert headers.get("x-flint-auth") == "secret-12345", headers
        assert headers.get("x-flint-extra") == "extra", headers
    finally:
        sb.kill()


# ── Timeout ────────────────────────────────────────────────────────────────


def test_set_timeout(sandbox):
    """Set a timeout on a sandbox and verify it is reflected in VM details."""
    sandbox.set_timeout(300, policy="kill")
    data = sandbox._fetch()
    assert data is not None
    assert data.get("timeout_seconds") == 300
    assert data.get("timeout_policy") == "kill"


# ── VM state & details ─────────────────────────────────────────────────────


def test_sandbox_state_property(sandbox):
    """Verify state transitions: running -> paused -> running."""
    assert sandbox.state in ("Starting", "Started", "Running")
    sandbox.pause()
    assert sandbox.state == "Paused"
    sandbox.resume()
    assert sandbox.state in ("Starting", "Started", "Running")


def test_get_vm_details(sandbox):
    """GET /vms/{vm_id} returns all expected fields."""
    data = sandbox._fetch()
    assert data is not None
    assert data["vm_id"] == sandbox.id
    assert "pid" in data and data["pid"] > 0
    assert "state" in data
    assert "created_at" in data
    assert "template_id" in data
    assert "backend_kind" in data


# ── Filesystem operations (extended) ───────────────────────────────────────


def test_file_stat(sandbox):
    """Stat a file and verify metadata is returned."""
    sandbox.write_file("/tmp/stat-test.txt", "hello")
    resp = api.get(
        f"{BASE_URL}/vms/{sandbox.id}/files/stat",
        params={"path": "/tmp/stat-test.txt"},
        timeout=10.0,
    )
    resp.raise_for_status()
    stat = resp.json()
    assert stat.get("size", stat.get("Size", 0)) > 0


def test_mkdir(sandbox):
    """Create a nested directory via the mkdir endpoint."""
    resp = api.post(
        f"{BASE_URL}/vms/{sandbox.id}/files/mkdir",
        params={"path": "/tmp/test-dir/nested", "parents": True},
        timeout=10.0,
    )
    resp.raise_for_status()
    result = sandbox.commands.run("test -d /tmp/test-dir/nested && echo ok")
    assert result.exit_code == 0
    assert "ok" in result.stdout


def test_delete_file(sandbox):
    """Write a file, delete it via API, verify it's gone."""
    sandbox.write_file("/tmp/del-test.txt", "to be deleted")
    resp = api.delete(
        f"{BASE_URL}/vms/{sandbox.id}/files",
        params={"path": "/tmp/del-test.txt"},
        timeout=10.0,
    )
    resp.raise_for_status()
    result = sandbox.commands.run("cat /tmp/del-test.txt 2>&1")
    assert result.exit_code != 0


def test_write_and_read_binary_file(sandbox):
    """Write binary content and read it back exactly."""
    data_in = b"\x00\x01\x02\x80\xff"
    sandbox.write_file("/tmp/bin.dat", data_in)
    data_out = sandbox.read_file("/tmp/bin.dat")
    assert data_out == data_in


def test_large_file_reads_stream(sandbox):
    """A file far bigger than the daemon holds in memory reads back whole."""
    size = 300 << 20
    sandbox.commands.run(f"truncate -s {size} /tmp/huge")  # sparse: no guest memory or disk
    received = 0
    with api.stream("GET", f"{BASE_URL}/vms/{sandbox.id}/files", params={"path": "/tmp/huge"}, timeout=120.0) as resp:
        assert resp.status_code == 200, resp.read()[:300]
        for chunk in resp.iter_bytes():
            assert not chunk.strip(b"\0"), "the file is all zeros"
            received += len(chunk)
    assert received == size


def test_large_file_writes_stream(sandbox):
    """A file far bigger than the daemon holds in memory arrives whole."""
    import hashlib

    data = os.urandom(150 << 20)
    # Into a pipe, so the guest needs neither the memory nor the disk for it.
    started = sandbox.commands.run(
        "mkfifo /tmp/pipe && setsid sh -c 'md5sum < /tmp/pipe > /tmp/sum.part && mv /tmp/sum.part /tmp/sum' "
        ">/dev/null 2>&1 </dev/null &")
    assert started.exit_code == 0, started.stderr
    sandbox.write_file("/tmp/pipe", data)
    deadline = time.monotonic() + 30
    while (result := sandbox.commands.run("cat /tmp/sum")).exit_code != 0:
        assert time.monotonic() < deadline, f"the guest never finished reading: {result.stderr}"
        time.sleep(0.2)
    assert result.stdout.split()[0] == hashlib.md5(data).hexdigest()


def test_write_file_with_mode(sandbox):
    """Write a file with executable mode and run it."""
    sandbox.write_file("/tmp/script.sh", "#!/bin/sh\necho mode_ok", mode="0755")
    result = sandbox.commands.run("/tmp/script.sh")
    assert result.exit_code == 0
    assert "mode_ok" in result.stdout


# ── Template management ────────────────────────────────────────────────────


def test_list_templates():
    """List templates and verify the default template exists."""
    resp = api.get(f"{BASE_URL}/templates", timeout=5.0)
    resp.raise_for_status()
    data = resp.json()
    assert "templates" in data
    assert isinstance(data["templates"], list)
    ids = [t.get("template_id", t.get("id")) for t in data["templates"]]
    assert "default" in ids


def test_get_template_default():
    """Get the default template by ID."""
    resp = api.get(f"{BASE_URL}/templates/default", timeout=5.0)
    resp.raise_for_status()
    tmpl = resp.json()["template"]
    assert tmpl is not None


def test_get_template_not_found():
    """Requesting a nonexistent template returns 404."""
    resp = api.get(
        f"{BASE_URL}/templates/nonexistent-template-xyz",
        timeout=5.0,
    )
    assert resp.status_code == 404


# ── Process management ─────────────────────────────────────────────────────


def test_list_processes(sandbox):
    """List processes running inside the sandbox."""
    resp = api.get(
        f"{BASE_URL}/vms/{sandbox.id}/processes",
        timeout=10.0,
    )
    # Guest agent may or may not implement this endpoint
    if resp.status_code == 404:
        pytest.skip("Guest agent does not support /processes endpoint")
    resp.raise_for_status()
    data = resp.json()
    assert isinstance(data, (list, dict))


def test_create_process(sandbox):
    """Spawn a process inside the sandbox and verify it returns a PID."""
    resp = api.post(
        f"{BASE_URL}/vms/{sandbox.id}/processes",
        json={"cmd": ["/bin/sh", "-c", "echo proc_ok"]},
        timeout=10.0,
    )
    if resp.status_code == 404:
        pytest.skip("Guest agent does not support /processes endpoint")
    resp.raise_for_status()
    data = resp.json()
    assert "pid" in data or "id" in data


# ── Additional happy-path flows ────────────────────────────────────────────


def test_multiple_pause_resume_cycles(sandbox):
    """Pause and resume twice — memory written before each pause survives both cycles."""
    # tmpfs keeps the data in guest RAM only, spread over thousands of pages.
    setup = sandbox.commands.run("mkdir -p /run/mem && mount -t tmpfs -o size=32m tmpfs /run/mem")
    assert setup.exit_code == 0, setup

    digests = {}
    for cycle in (1, 2):
        written = sandbox.commands.run(
            f"dd if=/dev/urandom of=/run/mem/c{cycle} bs=1M count=8 2>/dev/null && md5sum /run/mem/c{cycle}"
        )
        assert written.exit_code == 0, written
        digests[f"/run/mem/c{cycle}"] = written.stdout.split()[0]
        sandbox.pause()
        sandbox.resume()
        check = sandbox.commands.run("md5sum /run/mem/c*")
        assert check.exit_code == 0, check
        seen = {path: digest for digest, path in (line.split() for line in check.stdout.splitlines())}
        assert seen == digests, f"after cycle {cycle}: {check.stdout!r} != {digests}"


def test_create_sandbox_with_options(require_sandbox):
    """Create a sandbox with non-default options."""
    sb = Sandbox(allow_internet_access=False)
    try:
        assert sb.id
        assert sb.is_running()
        result = sb.commands.run("echo options_ok")
        assert result.exit_code == 0
        assert "options_ok" in result.stdout
    finally:
        sb.kill()


def test_list_excludes_killed_sandboxes(require_sandbox):
    """Sandboxes killed at the same time (as the web UI's bulk kill does) all go away cleanly."""
    import concurrent.futures

    boxes = [Sandbox() for _ in range(8)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        # Raw concurrent DELETEs, like the web UI sends; the SDK serialises its own requests.
        kills = list(pool.map(lambda sb: api.delete(f"{BASE_URL}/vms/{sb.id}", timeout=60.0), boxes))
    errors = [f"{r.status_code} {r.text[:200]}" for r in kills if r.status_code != 200]
    assert not errors, f"concurrent kills failed: {errors}"
    listed = {s.id for s in Sandbox.list()}
    assert not listed & {sb.id for sb in boxes}, f"killed sandboxes still listed: {listed}"

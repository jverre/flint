"""End-to-end tests for Flint sandboxes.

Requires a running daemon (`flint start`).
"""

import json
import os
import platform
import shutil
import threading
import time

import httpx
import pytest

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
    port = os.environ["FLINT_PORT"]
    resp = httpx.get(f"http://127.0.0.1:{port}/health", timeout=5.0)
    resp.raise_for_status()
    assert resp.json()["backend_kind"] == backend_kind


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
    """Concurrent commands get independent stdout/stderr."""
    import concurrent.futures

    def slow_cmd(n):
        # Use sleep 1 (busybox doesn't support fractional seconds)
        return sandbox.commands.run(f"sleep 1 && echo result-{n}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(slow_cmd, i) for i in range(3)]
        results = [f.result() for f in futures]

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


# ── run_command / run_code ───────────────────────────────────────────────────


def test_run_command(sandbox):
    """Test the high-level run_command method."""
    result = sandbox.run_command("echo hi")
    assert result.exit_code == 0
    assert "hi" in result.stdout


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
    port = os.environ["FLINT_PORT"]
    resp = httpx.get(f"http://127.0.0.1:{port}/health", timeout=5.0)
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


@pytest.mark.slow
def test_template_build_and_run(backend_kind):
    if backend_kind != "linux-firecracker":
        pytest.skip("Template build-and-run test is only implemented for the Linux backend today")
    if shutil.which("docker") is None:
        pytest.skip("docker is required for template build tests")

    template = Template("flint-test-template").from_alpine_image().run_cmd("touch /tmp/template-built").build()
    sandbox = Sandbox(template_id=template.template_id)
    try:
        result = sandbox.commands.run("test -f /tmp/template-built && echo ok")
        assert result.exit_code == 0
        assert "ok" in result.stdout
    finally:
        sandbox.kill()


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


def test_network_policy_round_trip(sandbox):
    """Set a network policy, read it back, verify it matches."""
    policy = {
        "allow": {
            "api.example.com": [
                {"transform": [{"headers": {"Authorization": "Bearer test-token"}}]}
            ],
            "*.internal.io": [
                {"transform": [{"headers": {"X-Api-Key": "key-123"}}]}
            ],
        }
    }
    sandbox.update_network_policy(policy)
    got = sandbox.get_network_policy()
    assert got is not None
    assert "allow" in got
    assert "api.example.com" in got["allow"]
    assert "*.internal.io" in got["allow"]
    rules = got["allow"]["api.example.com"]
    assert rules[0]["transform"][0]["headers"]["Authorization"] == "Bearer test-token"


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
    port = os.environ["FLINT_PORT"]
    resp = httpx.get(
        f"http://127.0.0.1:{port}/vms/{sandbox.id}/files/stat",
        params={"path": "/tmp/stat-test.txt"},
        timeout=10.0,
    )
    resp.raise_for_status()
    stat = resp.json()
    assert stat.get("size", stat.get("Size", 0)) > 0


def test_mkdir(sandbox):
    """Create a nested directory via the mkdir endpoint."""
    port = os.environ["FLINT_PORT"]
    resp = httpx.post(
        f"http://127.0.0.1:{port}/vms/{sandbox.id}/files/mkdir",
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
    port = os.environ["FLINT_PORT"]
    resp = httpx.delete(
        f"http://127.0.0.1:{port}/vms/{sandbox.id}/files",
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


def test_write_file_with_mode(sandbox):
    """Write a file with executable mode and run it."""
    sandbox.write_file("/tmp/script.sh", "#!/bin/sh\necho mode_ok", mode="0755")
    result = sandbox.commands.run("/tmp/script.sh")
    assert result.exit_code == 0
    assert "mode_ok" in result.stdout


# ── Template management ────────────────────────────────────────────────────


def test_list_templates():
    """List templates and verify the default template exists."""
    port = os.environ["FLINT_PORT"]
    resp = httpx.get(f"http://127.0.0.1:{port}/templates", timeout=5.0)
    resp.raise_for_status()
    data = resp.json()
    assert "templates" in data
    assert isinstance(data["templates"], list)
    ids = [t.get("template_id", t.get("id")) for t in data["templates"]]
    assert "default" in ids


def test_get_template_default():
    """Get the default template by ID."""
    port = os.environ["FLINT_PORT"]
    resp = httpx.get(f"http://127.0.0.1:{port}/templates/default", timeout=5.0)
    resp.raise_for_status()
    tmpl = resp.json()["template"]
    assert tmpl is not None


def test_get_template_not_found():
    """Requesting a nonexistent template returns 404."""
    port = os.environ["FLINT_PORT"]
    resp = httpx.get(
        f"http://127.0.0.1:{port}/templates/nonexistent-template-xyz",
        timeout=5.0,
    )
    assert resp.status_code == 404


# ── Process management ─────────────────────────────────────────────────────


def test_list_processes(sandbox):
    """List processes running inside the sandbox."""
    port = os.environ["FLINT_PORT"]
    resp = httpx.get(
        f"http://127.0.0.1:{port}/vms/{sandbox.id}/processes",
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
    port = os.environ["FLINT_PORT"]
    resp = httpx.post(
        f"http://127.0.0.1:{port}/vms/{sandbox.id}/processes",
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


def test_list_excludes_killed_sandbox(require_sandbox):
    """After killing a sandbox, it should not appear as running in the list."""
    sb = Sandbox()
    sb_id = sb.id
    sb.kill()
    time.sleep(0.5)
    running_ids = [s.id for s in Sandbox.list() if s.is_running()]
    assert sb_id not in running_ids

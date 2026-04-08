"""End-to-end tests for Flint sandboxes.

Requires a running daemon (`flint start`).
"""

import os
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


def test_multiple_sandboxes():
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


def test_workspace_isolation():
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

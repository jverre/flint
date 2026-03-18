"""End-to-end tests for Flint sandboxes.

Requires a running daemon (`flint start`).
"""

import threading
import time

from flint import Sandbox


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


# ── Jailer isolation ──────────────────────────────────────────────────────────


def test_jailer_chroot_cleanup(sandbox):
    """Verify the jailer chroot dir and cgroups are removed after kill."""
    import os
    from flint.core._jailer import JailSpec

    vm_id = sandbox.id
    spec = JailSpec(vm_id=vm_id, ns_name=f"fc-{vm_id[:8]}")
    chroot_base = spec.chroot_base

    assert os.path.isdir(chroot_base), "Chroot should exist while VM is running"
    sandbox.kill()
    time.sleep(0.5)
    assert not os.path.exists(chroot_base), "Chroot should be fully removed after kill"


def test_jailer_chroot_structure(sandbox):
    """Verify expected files exist inside the chroot while VM is running."""
    import os
    from flint.core._jailer import JailSpec

    vm_id = sandbox.id
    spec = JailSpec(vm_id=vm_id, ns_name=f"fc-{vm_id[:8]}")

    assert os.path.isfile(f"{spec.chroot_root}/rootfs.ext4"), "rootfs should be staged in chroot"
    assert os.path.exists(f"{spec.chroot_root}/firecracker.sock"), "API socket should exist"

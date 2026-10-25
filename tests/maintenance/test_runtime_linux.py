"""Recovery and bounded worker lifecycles with real Linux processes/cgroups."""
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("FLINT_PRIVILEGED_TESTS") != "1", reason="isolated privileged Linux container required")


@pytest.fixture(scope="module", autouse=True)
def delegated_cgroups():
    assert Path("/.dockerenv").exists(), "never remount a real host's cgroup root in this rehearsal"
    assert Path("/proc/self/cgroup").read_text().strip() == "0::/", "private cgroup namespace required"
    subprocess.run(["mount", "-o", "remount,rw", "/sys/fs/cgroup"], check=True, capture_output=True)
    supervisor = Path("/sys/fs/cgroup/flint-test-supervisor")
    supervisor.mkdir()
    for pid in Path("/sys/fs/cgroup/cgroup.procs").read_text().split():
        try:
            (supervisor / "cgroup.procs").write_text(pid)
        except ProcessLookupError:
            pass
    Path("/sys/fs/cgroup/cgroup.subtree_control").write_text("+cpu +memory +io +pids")
    yield


def test_deletion_recovery_stops_serving_and_retries_until_private_data_is_wiped(tmp_path):
    from flint.core._jailer import JailSpec
    from flint.core._limits import vm_cgroup
    from flint.core._netns import ensure_host_firewall
    from flint.core._state_store import StateStore
    from flint.core._recovery import RecoveryEngine
    from flint.core._health import HealthMonitor
    from flint.core.manager import SandboxManager
    from flint.core.backends.linux_firecracker import LinuxFirecrackerBackend
    from flint.core.types import SandboxState

    vm_id = str(uuid.uuid4())
    spec = JailSpec(vm_id, "review-" + vm_id[:8])
    root = Path(spec.chroot_root)
    root.mkdir(parents=True)
    (root / "tmp").mkdir()
    target = root / "service"
    subprocess.run(["go", "build", "-o", str(target), "tests/fixtures/peer_server.go"],
                   env={**os.environ, "CGO_ENABLED": "0"}, check=True, capture_output=True, timeout=60)
    disk = root / "rootfs.ext4"
    disk.write_bytes(b"tenant secret" * 4096)
    alias = tmp_path / "outside-link"
    os.link(disk, alias)
    subprocess.run(["ip", "netns", "add", spec.ns_name], check=True)
    subprocess.run(["ip", "-n", spec.ns_name, "link", "set", "lo", "up"], check=True)
    subprocess.run(["ip", "-n", spec.ns_name, "addr", "add", "172.16.0.2/32", "dev", "lo"], check=True)
    process = subprocess.Popen(["ip", "netns", "exec", spec.ns_name, "chroot", str(root), "/service"],
                               env={**os.environ, "FLINT_TEST_LISTEN": "0.0.0.0:5000"})
    store = StateStore(str(tmp_path / "state.db"))
    try:
        deadline = time.monotonic() + 5
        while not (root / "tmp/peer-ready").exists():
            assert process.poll() is None and time.monotonic() < deadline
            time.sleep(.01)
        store.insert_sandbox(vm_id, process.pid, str(root), spec.socket_path_on_host, spec.ns_name,
                             SandboxState.RUNNING, os.getpid(), chroot_base=spec.chroot_base, tenant="a")
        store.transition_state(vm_id, SandboxState.DELETING, detail="deletion pending")
        ensure_host_firewall()
        backend = LinuxFirecrackerBackend()
        with disk.open("rb") as held:
            manager = SandboxManager(backend, store)
            RecoveryEngine(store, manager).recover()
            process.wait(timeout=5)
            assert manager.get_entry(vm_id) is None
            row = store.get_sandbox(vm_id)
            assert row["state"] == "Deleting" and row["pid"] == 0
            assert root.exists(), "failed wipe must retain its recovery path"
            state_age = row["state_changed_at"]
            HealthMonitor(store, manager)._check_all()
            store.update_health(vm_id)
            assert store.get_sandbox(vm_id)["state_changed_at"] == state_age
            assert held.read() == b"tenant secret" * 4096
            alias.unlink()
            # A second daemon recovery resumes cleanup, never normal serving.
            manager = SandboxManager(backend, store)
            RecoveryEngine(store, manager).recover()
            assert manager.get_entry(vm_id) is None
            assert store.get_sandbox(vm_id)["state"] == "Dead"
            assert not root.exists() and not Path(vm_cgroup(vm_id, backend.kind)).exists()
            held.seek(0)
            assert set(held.read()) == {0}
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        store.close()


def test_build_deadline_and_admission_bound_the_entire_worker_lifetime(tmp_path, monkeypatch):
    from flint.core import config
    from flint.core import _build_job as jobs
    from flint.core._limits import cgroup_limits
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path / "data"))
    job = jobs._root() / uuid.uuid4().hex
    job.mkdir()
    jobs._atomic(job / "job.json", {"template_id": "test", "kind": "linux-firecracker"})
    group = f"/sys/fs/cgroup/flint-builds/{job.name}"
    cgroup_limits(group, str(tmp_path), vmm_disk_limit=True)
    script = """
import os,sys,time,subprocess
from flint.core._limits import attach_process
attach_process(sys.argv[1], os.getpid())
subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
while True:
 print('still responding', flush=True)
 time.sleep(.02)
"""
    started = time.monotonic()
    with jobs.Admission():
        with pytest.raises(jobs.BuildBusy):
            jobs.Admission()
        with (job / "worker.log").open("wb") as log:
            with pytest.raises(TimeoutError, match="host deadline"):
                jobs._supervise([sys.executable, "-c", script, group], b"", log, os.environ.copy(), .5)
        jobs._cleanup(job)
    assert time.monotonic() - started < 5
    assert not Path(group).exists(), "the worker's child must also be gone"
    assert b"still responding" in (job / "worker.log").read_bytes()
    with jobs.Admission():
        pass
    jobs._finish(job)

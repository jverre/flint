from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass, field

from .config import log, GOLDEN_DIR, GOLDEN_TAP, DEFAULT_TEMPLATE_ID, JAILER_UID, JAILER_GID
from ._netns import _ns_name, _delete_netns, _setup_netns_pyroute2, _setup_netns_subprocess
from ._firecracker import _wait_for_api_socket, _fc_put, _fc_patch, _fc_status_ok, _wait_for_agent
from ._pool import _claim_pool_entry
from ._template_registry import get_template_dir
from ._jailer import JailSpec, stage_file_into_chroot, build_jailer_command, cleanup_jailer


@dataclass
class BootResult:
    vm_id: str
    vm_dir: str          # chroot_root
    socket_path: str     # absolute path on host
    ns_name: str
    process: subprocess.Popen
    agent_url: str
    chroot_base: str
    timings: dict[str, float] = field(default_factory=dict)
    t_total: float = 0.0


class _RecoveredProcess:
    """Lightweight process handle for VMs recovered after daemon restart."""

    def __init__(self, pid: int) -> None:
        self.pid = pid

    def kill(self) -> None:
        try:
            os.kill(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def wait(self, timeout: float | None = None) -> None:
        deadline = time.monotonic() + (timeout or 10)
        while time.monotonic() < deadline:
            try:
                os.kill(self.pid, 0)
                time.sleep(0.1)
            except ProcessLookupError:
                return


def _timed(timings: dict, key: str):
    """Context manager to time a block and store result in timings dict."""
    class _Timer:
        def __enter__(self):
            self.t0 = time.monotonic()
            return self
        def __exit__(self, *_):
            timings[key] = (time.monotonic() - self.t0) * 1000
    return _Timer()


def _boot_from_snapshot(
    *,
    template_id: str = DEFAULT_TEMPLATE_ID,
    allow_internet_access: bool = True,
    use_pool: bool = True,
    use_pyroute2: bool = True,
    network_overrides: list[dict] | None = None,
) -> BootResult:
    """Boot a VM from a template snapshot via the jailer. Returns BootResult.

    On failure, cleans up all resources and raises.
    On success, caller owns the process/netns/chroot and must clean up.
    """
    vm_id = str(uuid.uuid4())
    ns_name = _ns_name(vm_id)
    spec = JailSpec(vm_id=vm_id, ns_name=ns_name)
    sid = vm_id[:8]

    snapshot_dir = get_template_dir(template_id) if template_id != DEFAULT_TEMPLATE_ID else GOLDEN_DIR

    timings = {}
    t_total = time.monotonic()
    process = None
    pool_entry_dir = None

    try:
        # 1. Create chroot root directory
        os.makedirs(spec.chroot_root, exist_ok=True)
        os.chown(spec.chroot_root, JAILER_UID, JAILER_GID)

        # 2. Stage rootfs into chroot
        with _timed(timings, "copy_rootfs_ms"):
            if use_pool:
                pool_entry_dir = _claim_pool_entry(template_id)
            rootfs_src = f"{pool_entry_dir}/rootfs.ext4" if pool_entry_dir else f"{snapshot_dir}/rootfs.ext4"
            stage_file_into_chroot(rootfs_src, "rootfs.ext4", spec)
            if pool_entry_dir:
                shutil.rmtree(pool_entry_dir, ignore_errors=True)
                pool_entry_dir = None
            # The vmstate bakes in the absolute drive path from when the snapshot was
            # created (e.g. /microvms/.golden/rootfs.ext4). Firecracker running inside
            # the jailer chroot resolves absolute symlink targets relative to the chroot
            # root, so a symlink at that path pointing to /rootfs.ext4 makes it find the
            # staged file without requiring the original host path to exist in the chroot.
            _snapshot_drive_relpath = snapshot_dir.lstrip("/") + "/rootfs.ext4"
            _snapshot_drive_in_chroot = os.path.join(spec.chroot_root, _snapshot_drive_relpath)
            os.makedirs(os.path.dirname(_snapshot_drive_in_chroot), exist_ok=True)
            os.symlink("/rootfs.ext4", _snapshot_drive_in_chroot)

        # 3. Create network namespace + TAP
        with _timed(timings, "netns_setup_ms"):
            if use_pyroute2:
                _setup_netns_pyroute2(ns_name, GOLDEN_TAP, internet=allow_internet_access)
            else:
                _setup_netns_subprocess(ns_name, GOLDEN_TAP, internet=allow_internet_access)

        # 4. Stage snapshot files into chroot
        with _timed(timings, "stage_snapshot_ms"):
            stage_file_into_chroot(f"{snapshot_dir}/vmstate", "vmstate", spec)
            stage_file_into_chroot(f"{snapshot_dir}/mem", "mem", spec)

        # 5. Start jailer (jailer handles netns entry via --netns flag)
        with _timed(timings, "popen_ms"):
            log_path = f"{spec.chroot_root}/firecracker.log"
            with open(log_path, "w") as log_fd:
                process = subprocess.Popen(
                    build_jailer_command(spec),
                    stdin=subprocess.DEVNULL, stdout=log_fd, stderr=subprocess.STDOUT,
                    start_new_session=True,
                )

        # 6. Wait for API socket
        with _timed(timings, "wait_api_ready_ms"):
            _wait_for_api_socket(spec.socket_path_on_host)

        # 7. Load snapshot (paths are chroot-relative)
        with _timed(timings, "api_snapshot_load_ms"):
            snapshot_body = {
                "snapshot_path": "vmstate",
                "mem_backend": {"backend_type": "File", "backend_path": "mem"},
                "enable_diff_snapshots": False,
                "resume_vm": False,
            }
            if network_overrides:
                snapshot_body["network_overrides"] = network_overrides
            resp = _fc_put(spec.socket_path_on_host, "/snapshot/load", snapshot_body)
        if not _fc_status_ok(resp):
            raise RuntimeError(f"snapshot/load failed: {resp}")

        # 8. Patch drives (chroot-relative)
        with _timed(timings, "api_drives_ms"):
            _fc_patch(spec.socket_path_on_host, "/drives/rootfs", {"drive_id": "rootfs", "path_on_host": "rootfs.ext4"})

        # 9. Resume
        with _timed(timings, "api_resume_ms"):
            _fc_patch(spec.socket_path_on_host, "/vm", {"state": "Resumed"})

        # 10. Wait for guest agent
        with _timed(timings, "agent_connect_ms"):
            agent_url = _wait_for_agent(ns_name)

        total_ms = (time.monotonic() - t_total) * 1000
        parts = " | ".join(f"{k}={v:.1f}" for k, v in timings.items())
        log.debug("[%s] boot %.0f ms: %s", sid, total_ms, parts)

        return BootResult(
            vm_id=vm_id,
            vm_dir=spec.chroot_root,
            socket_path=spec.socket_path_on_host,
            ns_name=ns_name,
            process=process,
            agent_url=agent_url,
            chroot_base=spec.chroot_base,
            timings=timings,
            t_total=t_total,
        )

    except:
        if process:
            process.kill()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        if pool_entry_dir:
            shutil.rmtree(pool_entry_dir, ignore_errors=True)
        _delete_netns(ns_name)
        cleanup_jailer(spec.chroot_base, vm_id)
        raise


def _teardown_vm(process, ns_name: str, chroot_base: str, vm_id: str) -> None:
    """Kill process, delete netns, remove jailer chroot and cgroups."""
    if process:
        process.kill()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
    _delete_netns(ns_name)
    cleanup_jailer(chroot_base, vm_id)

import os
import shutil
import subprocess
import time
import uuid

from .config import log, GOLDEN_DIR, GOLDEN_TAP
from ._netns import _ns_name, _delete_netns, _popen_in_ns, _setup_netns_pyroute2, _setup_netns_subprocess
from ._firecracker import _wait_for_api_socket, _fc_put, _fc_patch, _fc_status_ok, _tcp_connect
from ._pool import _claim_pool_entry


def _timed(timings: dict, key: str):
    """Context manager to time a block and store result in timings dict."""
    class _Timer:
        def __enter__(self):
            self.t0 = time.monotonic()
            return self
        def __exit__(self, *_):
            timings[key] = (time.monotonic() - self.t0) * 1000
    return _Timer()


def _prepare_rootfs(vm_id: str, vm_dir: str, rootfs_path: str, use_pool: bool) -> tuple[str, str, str]:
    """Copy or claim rootfs. Returns (vm_dir, rootfs_path, socket_path)."""
    if use_pool:
        claimed_dir = _claim_pool_entry("golden", vm_id)
        if claimed_dir:
            return claimed_dir, f"{claimed_dir}/rootfs.ext4", f"{claimed_dir}/firecracker.sock"
    os.makedirs(vm_dir, exist_ok=True)
    subprocess.run(["cp", "--reflink=auto", f"{GOLDEN_DIR}/rootfs.ext4", rootfs_path], check=True)
    return vm_dir, rootfs_path, f"{vm_dir}/firecracker.sock"


def _boot_from_snapshot(
    *,
    use_pool: bool = True,
    use_pyroute2: bool = True,
    network_overrides: list[dict] | None = None,
) -> dict:
    """Boot a VM from golden snapshot. Returns dict with VM info.

    On failure, cleans up all resources and raises.
    On success, caller owns the process/netns/dir and must clean up.
    """
    vm_id = str(uuid.uuid4())
    vm_dir = f"/microvms/{vm_id}"
    rootfs_path = f"{vm_dir}/rootfs.ext4"
    ns_name = _ns_name(vm_id)
    sid = vm_id[:8]

    timings = {}
    t_total = time.monotonic()
    process = None

    try:
        # 1. Copy rootfs
        with _timed(timings, "copy_rootfs_ms"):
            vm_dir, rootfs_path, socket_path = _prepare_rootfs(vm_id, vm_dir, rootfs_path, use_pool)

        # 2. Create network namespace + TAP
        with _timed(timings, "netns_setup_ms"):
            if use_pyroute2:
                _setup_netns_pyroute2(ns_name, GOLDEN_TAP)
            else:
                _setup_netns_subprocess(ns_name, GOLDEN_TAP)

        # 3. Start Firecracker
        with _timed(timings, "popen_ms"):
            log_path = f"{vm_dir}/firecracker.log"
            with open(log_path, "w") as log_fd:
                process = _popen_in_ns(
                    ns_name,
                    ["firecracker", "--api-sock", socket_path, "--id", vm_id],
                    stdin=subprocess.DEVNULL, stdout=log_fd, stderr=subprocess.STDOUT,
                    start_new_session=True,
                )

        # 4. Wait for API socket
        with _timed(timings, "wait_api_ready_ms"):
            _wait_for_api_socket(socket_path)

        # 5. Load snapshot
        with _timed(timings, "api_snapshot_load_ms"):
            snapshot_body = {
                "snapshot_path": f"{GOLDEN_DIR}/vmstate",
                "mem_backend": {"backend_type": "File", "backend_path": f"{GOLDEN_DIR}/mem"},
                "enable_diff_snapshots": False,
                "resume_vm": False,
            }
            if network_overrides:
                snapshot_body["network_overrides"] = network_overrides
            resp = _fc_put(socket_path, "/snapshot/load", snapshot_body)
        if not _fc_status_ok(resp):
            raise RuntimeError(f"snapshot/load failed: {resp}")

        # 6. Patch drives
        with _timed(timings, "api_drives_ms"):
            _fc_patch(socket_path, "/drives/rootfs", {"drive_id": "rootfs", "path_on_host": rootfs_path})

        # 7. Resume
        with _timed(timings, "api_resume_ms"):
            _fc_patch(socket_path, "/vm", {"state": "Resumed"})

        # 8. TCP connect
        with _timed(timings, "tcp_connect_ms"):
            tcp_sock = _tcp_connect(ns_name)

        total_ms = (time.monotonic() - t_total) * 1000
        parts = " | ".join(f"{k}={v:.1f}" for k, v in timings.items())
        log.debug("[%s] boot %.0f ms: %s", sid, total_ms, parts)

        return {
            "vm_id": vm_id, "vm_dir": vm_dir, "socket_path": socket_path,
            "ns_name": ns_name, "process": process, "tcp_socket": tcp_sock,
            "timings": timings, "t_total": t_total,
        }

    except:
        if process:
            process.kill()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        _delete_netns(ns_name)
        shutil.rmtree(vm_dir, ignore_errors=True)
        raise


def _teardown_vm(process, ns_name: str, vm_dir: str) -> None:
    """Kill process, delete netns, remove VM directory."""
    if process:
        process.kill()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
    _delete_netns(ns_name)
    shutil.rmtree(vm_dir, ignore_errors=True)

import os
import shutil
import subprocess
import time
import urllib.request

from .config import (
    log, KERNEL_PATH, BOOT_ARGS, GUEST_MAC, GOLDEN_TAP,
    GOLDEN_NS, GOLDEN_DIR, GUEST_IP, AGENT_PORT, DATA_DIR,
)
from ._netns import _delete_netns, _popen_in_ns, _setup_netns_pyroute2, _enter_netns, _restore_netns
from ._firecracker import _wait_for_api_socket, _fc_put, _fc_patch, _fc_status_ok


def golden_snapshot_exists() -> bool:
    return all(os.path.exists(f"{GOLDEN_DIR}/{f}") for f in ("rootfs.ext4", "vmstate", "mem"))


def _golden_cleanup(process, ns_name, vm_dir):
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
    _delete_netns(ns_name)
    shutil.rmtree(vm_dir, ignore_errors=True)


def create_golden_snapshot(
    *,
    source_rootfs: str,
    snapshot_dir: str = GOLDEN_DIR,
    ns_name: str = GOLDEN_NS,
    tap_name: str = GOLDEN_TAP,
) -> None:
    """Boot a VM, wait for READY, verify agent health, pause, snapshot, store as golden.

    Parameters are configurable to support per-template snapshots.
    """
    log.info("Creating golden snapshot (dir=%s)...", snapshot_dir)
    vm_id = "golden-snapshot"
    vm_dir = f"{DATA_DIR}/{vm_id}"
    socket_path = f"{vm_dir}/firecracker.sock"
    rootfs_path = f"{vm_dir}/rootfs.ext4"

    # 1. Kill stale processes
    if os.path.exists(socket_path):
        subprocess.run(["fuser", "-k", socket_path], capture_output=True)
        time.sleep(0.1)

    # 2. Prepare rootfs
    _delete_netns(ns_name)
    shutil.rmtree(vm_dir, ignore_errors=True)
    os.makedirs(vm_dir, exist_ok=True)
    subprocess.run(["cp", "--reflink=auto", source_rootfs, rootfs_path], check=True)

    # 3. Create namespace + TAP
    _setup_netns_pyroute2(ns_name, tap_name)

    # 4. Start Firecracker in the namespace
    log_path = f"{vm_dir}/firecracker.log"
    with open(log_path, "w") as log_fd:
        process = _popen_in_ns(
            ns_name,
            ["firecracker", "--boot-timer", "--api-sock", socket_path, "--id", vm_id],
            stdin=subprocess.DEVNULL, stdout=log_fd, stderr=subprocess.STDOUT,
        )
    log.info("Golden VM started in ns %s (pid=%d)", ns_name, process.pid)

    # 5. Configure VM via API
    _wait_for_api_socket(socket_path)
    _fc_put(socket_path, "/boot-source", {"kernel_image_path": KERNEL_PATH, "boot_args": BOOT_ARGS})

    os.makedirs(snapshot_dir, exist_ok=True)
    golden_rootfs_path = f"{snapshot_dir}/rootfs.ext4"
    shutil.copy2(rootfs_path, golden_rootfs_path)
    _fc_put(socket_path, "/drives/rootfs", {
        "drive_id": "rootfs", "path_on_host": golden_rootfs_path,
        "is_root_device": True, "is_read_only": False,
    })
    _fc_put(socket_path, "/network-interfaces/eth0", {
        "iface_id": "eth0", "guest_mac": GUEST_MAC, "host_dev_name": tap_name,
    })
    _fc_put(socket_path, "/actions", {"action_type": "InstanceStart"})

    # 6. Wait for READY
    t0 = time.monotonic()
    with open(log_path, "r") as f:
        while True:
            line = f.readline()
            if not line:
                if time.monotonic() - t0 > 10:
                    _golden_cleanup(process, ns_name, vm_dir)
                    raise TimeoutError("READY not detected within timeout")
                time.sleep(0.01)
                continue
            if "READY" in line:
                break
    log.info("Golden VM ready (%.0f ms)", (time.monotonic() - t0) * 1000)

    # 7. Verify flintd agent health and warm up
    t0_agent = time.monotonic()
    agent_url = f"http://{GUEST_IP}:{AGENT_PORT}"
    orig_fd = _enter_netns(ns_name)
    connected = False
    last_err = None
    try:
        for attempt in range(300):
            try:
                req = urllib.request.Request(f"{agent_url}/health", method="GET")
                with urllib.request.urlopen(req, timeout=2.0) as resp:
                    if resp.status == 200:
                        connected = True
                        log.info("Agent health OK (%.0f ms, attempt %d)",
                                 (time.monotonic() - t0_agent) * 1000, attempt + 1)
                        break
            except Exception as e:
                last_err = e
                time.sleep(0.02)

        # Warm up with a simple exec (best-effort, don't fail on error)
        if connected:
            import json
            exec_body = json.dumps({"cmd": ["/bin/sh", "-c", "echo WARM"], "timeout": 5}).encode()
            exec_req = urllib.request.Request(f"{agent_url}/exec", data=exec_body, method="POST")
            exec_req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(exec_req, timeout=5.0) as exec_resp:
                    result = exec_resp.read()
                    log.info("Agent warm-up exec result: %s", result.decode(errors="replace"))
            except Exception as e:
                log.warning("Agent warm-up exec failed (non-fatal): %s", e)
    finally:
        _restore_netns(orig_fd)
    if not connected:
        log.error("Agent unreachable after %.0f ms, last error: %s",
                  (time.monotonic() - t0_agent) * 1000, last_err)
        _golden_cleanup(process, ns_name, vm_dir)
        raise TimeoutError(f"Golden VM flintd agent not reachable: {last_err}")

    # 8. Pause
    resp = _fc_patch(socket_path, "/vm", {"state": "Paused"})
    if not _fc_status_ok(resp):
        _golden_cleanup(process, ns_name, vm_dir)
        raise RuntimeError(f"Failed to pause golden VM: {resp}")
    log.info("Golden VM paused")

    # 9. Create snapshot
    resp = _fc_put(socket_path, "/snapshot/create", {
        "snapshot_type": "Full",
        "snapshot_path": f"{snapshot_dir}/vmstate",
        "mem_file_path": f"{snapshot_dir}/mem",
    })
    if not _fc_status_ok(resp):
        _golden_cleanup(process, ns_name, vm_dir)
        raise RuntimeError(f"Golden snapshot creation failed: {resp}")

    # 10. Verify snapshot files
    for fname in ("rootfs.ext4", "vmstate", "mem"):
        fpath = f"{snapshot_dir}/{fname}"
        if not os.path.exists(fpath):
            _golden_cleanup(process, ns_name, vm_dir)
            raise RuntimeError(f"Golden snapshot file missing: {fpath}")
        log.info("Golden %s: %d bytes", fname, os.path.getsize(fpath))

    # 11. Cleanup golden VM
    _golden_cleanup(process, ns_name, vm_dir)

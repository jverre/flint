import os
import shutil
import socket
import subprocess
import time

from .config import (
    log, SOURCE_ROOTFS, KERNEL_PATH, BOOT_ARGS, GUEST_MAC, GOLDEN_TAP,
    GOLDEN_NS, GOLDEN_DIR, GUEST_IP, TCP_PORT,
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


def create_golden_snapshot() -> None:
    """Boot a VM, wait for READY, pause, snapshot, store as golden."""
    log.info("Creating golden snapshot...")
    vm_id = "golden-snapshot"
    vm_dir = f"/microvms/{vm_id}"
    socket_path = f"{vm_dir}/firecracker.sock"
    rootfs_path = f"{vm_dir}/rootfs.ext4"

    # 1. Kill stale processes
    if os.path.exists(socket_path):
        subprocess.run(["fuser", "-k", socket_path], capture_output=True)
        time.sleep(0.1)

    # 2. Prepare rootfs
    _delete_netns(GOLDEN_NS)
    shutil.rmtree(vm_dir, ignore_errors=True)
    os.makedirs(vm_dir, exist_ok=True)
    subprocess.run(["cp", "--reflink=auto", SOURCE_ROOTFS, rootfs_path], check=True)

    # 3. Create namespace + TAP
    _setup_netns_pyroute2(GOLDEN_NS, GOLDEN_TAP)

    # 4. Start Firecracker in the namespace
    log_path = f"{vm_dir}/firecracker.log"
    with open(log_path, "w") as log_fd:
        process = _popen_in_ns(
            GOLDEN_NS,
            ["firecracker", "--boot-timer", "--api-sock", socket_path, "--id", vm_id],
            stdin=subprocess.DEVNULL, stdout=log_fd, stderr=subprocess.STDOUT,
        )
    log.info("Golden VM started in ns %s (pid=%d)", GOLDEN_NS, process.pid)

    # 5. Configure VM via API
    _wait_for_api_socket(socket_path)
    _fc_put(socket_path, "/boot-source", {"kernel_image_path": KERNEL_PATH, "boot_args": BOOT_ARGS})

    os.makedirs(GOLDEN_DIR, exist_ok=True)
    golden_rootfs_path = f"{GOLDEN_DIR}/rootfs.ext4"
    shutil.copy2(rootfs_path, golden_rootfs_path)
    _fc_put(socket_path, "/drives/rootfs", {
        "drive_id": "rootfs", "path_on_host": golden_rootfs_path,
        "is_root_device": True, "is_read_only": False,
    })
    _fc_put(socket_path, "/network-interfaces/eth0", {
        "iface_id": "eth0", "guest_mac": GUEST_MAC, "host_dev_name": GOLDEN_TAP,
    })
    _fc_put(socket_path, "/actions", {"action_type": "InstanceStart"})

    # 6. Wait for READY
    t0 = time.monotonic()
    with open(log_path, "r") as f:
        while True:
            line = f.readline()
            if not line:
                if time.monotonic() - t0 > 10:
                    _golden_cleanup(process, GOLDEN_NS, vm_dir)
                    raise TimeoutError("READY not detected within timeout")
                time.sleep(0.01)
                continue
            if "READY" in line:
                break
    log.info("Golden VM ready (%.0f ms)", (time.monotonic() - t0) * 1000)

    # 7. Verify TCP listener
    t0_tcp = time.monotonic()
    orig_fd = _enter_netns(GOLDEN_NS)
    connected = False
    try:
        for attempt in range(100):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.settimeout(0.1)
                sock.connect((GUEST_IP, TCP_PORT))
                sock.close()
                connected = True
                log.info("TCP listener confirmed (%.0f ms, attempt %d)",
                         (time.monotonic() - t0_tcp) * 1000, attempt + 1)
                break
            except (ConnectionRefusedError, TimeoutError, OSError):
                sock.close()
                time.sleep(0.01)
    finally:
        _restore_netns(orig_fd)
    if not connected:
        _golden_cleanup(process, GOLDEN_NS, vm_dir)
        raise TimeoutError("Golden VM TCP listener not reachable")

    # 8. Pause
    resp = _fc_patch(socket_path, "/vm", {"state": "Paused"})
    if not _fc_status_ok(resp):
        _golden_cleanup(process, GOLDEN_NS, vm_dir)
        raise RuntimeError(f"Failed to pause golden VM: {resp}")
    log.info("Golden VM paused")

    # 9. Create snapshot
    resp = _fc_put(socket_path, "/snapshot/create", {
        "snapshot_type": "Full",
        "snapshot_path": f"{GOLDEN_DIR}/vmstate",
        "mem_file_path": f"{GOLDEN_DIR}/mem",
    })
    if not _fc_status_ok(resp):
        _golden_cleanup(process, GOLDEN_NS, vm_dir)
        raise RuntimeError(f"Golden snapshot creation failed: {resp}")

    # 10. Verify snapshot files
    for fname in ("rootfs.ext4", "vmstate", "mem"):
        fpath = f"{GOLDEN_DIR}/{fname}"
        if not os.path.exists(fpath):
            _golden_cleanup(process, GOLDEN_NS, vm_dir)
            raise RuntimeError(f"Golden snapshot file missing: {fpath}")
        log.info("Golden %s: %d bytes", fname, os.path.getsize(fpath))

    # 11. Cleanup golden VM
    _golden_cleanup(process, GOLDEN_NS, vm_dir)

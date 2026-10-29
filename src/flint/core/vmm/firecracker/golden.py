"""The default template's golden snapshot for Firecracker: boot once, snapshot, keep."""

from __future__ import annotations

import json
import os
import time
import urllib.request
import uuid

from ...config import (
    log, SOURCE_ROOTFS, KERNEL_PATH, BOOT_ARGS, GUEST_MAC, GOLDEN_TAP,
    GOLDEN_NS, GOLDEN_DIR, GUEST_IP, AGENT_PORT,
)
from ...net.netns import _delete_netns, _setup_netns_pyroute2, _enter_netns, _restore_netns
from ..._artifacts import dig_holes, sparse_copy
from ..._jailfs import copy_out
from ..common import log_tail, stop_process
from . import api
from .boot import TEMPLATE_FILES
from .jail import FirecrackerJail


def golden_snapshot_exists() -> bool:
    return all(os.path.exists(f"{GOLDEN_DIR}/{f}") for f in TEMPLATE_FILES)


def create_golden_snapshot(
    source_rootfs: str = SOURCE_ROOTFS,
    snapshot_dir: str = GOLDEN_DIR,
    ns_name: str = GOLDEN_NS,
    tap_name: str = GOLDEN_TAP,
) -> None:
    """Boot a VM, wait for READY, verify agent health, pause, snapshot, store as golden.

    Parameters are configurable to support per-template snapshots. The VM runs
    through the jailer like any sandbox: template images are tenant-supplied,
    so this boot gets no more privilege than the sandboxes it seeds. Paths are
    chroot-relative, so the snapshot references ``rootfs.ext4`` exactly where
    every sandbox's jail has it.
    """
    log.info("Creating golden snapshot (dir=%s)...", snapshot_dir)
    jail = FirecrackerJail(f"golden-{uuid.uuid4().hex[:12]}")
    from ..._build_job import register_vm
    register_vm(jail.vm_id, ns_name, "linux-firecracker")
    socket_path = jail.socket_path
    process = None

    _delete_netns(ns_name)
    try:
        os.makedirs(jail.root)
        os.chown(jail.root, *jail.identity)
        jail.link_shared(KERNEL_PATH, "/vmlinux")
        sparse_copy(source_rootfs, jail.host_path("/rootfs.ext4"))
        jail.give("/rootfs.ext4")

        # The golden VM needs no internet; it only has to bring flintd up.
        _setup_netns_pyroute2(ns_name, tap_name, internet=False)

        process = jail.spawn(ns_name)
        log.info("Golden VM started in ns %s (pid=%d)", ns_name, process.pid)

        api.put(socket_path, "/boot-source", {"kernel_image_path": "vmlinux", "boot_args": BOOT_ARGS})
        api.put(socket_path, "/drives/rootfs", {
            "drive_id": "rootfs", "path_on_host": "rootfs.ext4",
            "is_root_device": True, "is_read_only": False,
        })
        api.put(socket_path, "/network-interfaces/eth0", {
            "iface_id": "eth0", "guest_mac": GUEST_MAC, "host_dev_name": tap_name,
        })
        api.put(socket_path, "/actions", {"action_type": "InstanceStart"})

        _wait_for_ready(jail.log_path)
        _wait_for_agent_and_warm_up(ns_name)

        resp = api.set_vm_state(socket_path, "Paused")
        if not api.status_ok(resp):
            raise RuntimeError(f"Failed to pause golden VM: {resp}")
        log.info("Golden VM paused")

        resp = api.put(socket_path, "/snapshot/create", {
            "snapshot_type": "Full", "snapshot_path": "vmstate", "mem_file_path": "mem",
        })
        if not api.status_ok(resp):
            raise RuntimeError(f"Golden snapshot creation failed: {resp}")

        # Copy out of the jail (without following links: the template image
        # ran in this VMM), then swap all files in at once.
        os.makedirs(snapshot_dir, exist_ok=True)
        for name in TEMPLATE_FILES:
            copy_out(jail.host_path(f"/{name}"), f"{snapshot_dir}/{name}.tmp")
        # Guest memory is mostly zeros; holes keep per-VM copies and page cache small.
        dig_holes(f"{snapshot_dir}/mem.tmp")
        for name in TEMPLATE_FILES:
            os.chmod(f"{snapshot_dir}/{name}.tmp", 0o644)
            os.replace(f"{snapshot_dir}/{name}.tmp", f"{snapshot_dir}/{name}")
            log.info("Golden %s: %d bytes", name, os.path.getsize(f"{snapshot_dir}/{name}"))
    except Exception:
        log.error("Golden snapshot failed — firecracker log:\n%s", log_tail(jail.log_path))
        raise
    finally:
        stop_process(process, timeout=5)
        _delete_netns(ns_name)
        jail.cleanup()
        for name in TEMPLATE_FILES:
            try:
                os.unlink(f"{snapshot_dir}/{name}.tmp")
            except FileNotFoundError:
                pass


def _wait_for_ready(log_path: str, timeout: float = 10.0) -> None:
    t0 = time.monotonic()
    seen = ""
    with open(log_path) as f:
        while True:
            chunk = f.read()
            if not chunk:
                if time.monotonic() - t0 > timeout:
                    raise TimeoutError("READY not detected within timeout")
                time.sleep(0.01)
                continue
            # The serial console arrives in fragments; a marker can span reads.
            seen = (seen + chunk)[-4096:]
            if "READY" in seen:
                break
    log.info("Golden VM ready (%.0f ms)", (time.monotonic() - t0) * 1000)


def _wait_for_agent_and_warm_up(ns_name: str) -> None:
    """Wait for flintd, then run one exec so the snapshot has a warm exec path."""
    t0 = time.monotonic()
    agent_url = f"http://{GUEST_IP}:{AGENT_PORT}"
    orig_fd = _enter_netns(ns_name)
    last_err = None
    try:
        for attempt in range(300):
            try:
                req = urllib.request.Request(f"{agent_url}/health", method="GET")
                with urllib.request.urlopen(req, timeout=2.0) as resp:
                    if resp.status == 200:
                        log.info("Agent health OK (%.0f ms, attempt %d)", (time.monotonic() - t0) * 1000, attempt + 1)
                        break
            except Exception as e:
                last_err = e
                time.sleep(0.02)
        else:
            raise TimeoutError(f"Golden VM flintd agent not reachable: {last_err}")

        # Best-effort: a failed warm-up doesn't make the snapshot unusable.
        exec_body = json.dumps({"cmd": ["/bin/sh", "-c", "echo WARM"], "timeout": 5}).encode()
        exec_req = urllib.request.Request(f"{agent_url}/exec", data=exec_body, method="POST")
        exec_req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(exec_req, timeout=5.0) as exec_resp:
                log.info("Agent warm-up exec result: %s", exec_resp.read().decode(errors="replace"))
        except Exception as e:
            log.warning("Agent warm-up exec failed (non-fatal): %s", e)
    finally:
        _restore_netns(orig_fd)

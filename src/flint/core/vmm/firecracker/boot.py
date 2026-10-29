"""Firecracker VMs: boot from a template snapshot, pause to and resume from a per-VM snapshot.

Every VM runs in a :class:`FirecrackerJail` and restores the template's
golden snapshot (``vmstate`` + ``mem``, hard-linked read-only into the jail)
onto its own copy of the disk, so it skips kernel boot.

Pause writes ``pause-vmstate``/``pause-mem`` into the jail. ``pause-mem``
always holds a complete memory image: a copy of the golden ``mem`` before the
first pause (pre-built by the pool), and the VM's own backing file after a
resume. A diff snapshot writes only the pages dirtied since the last restore
into it in place; clean pages are still backed by that file's page cache, so
rewriting dirty pages under the running VM is safe. A full snapshot is the
fallback.
"""

from __future__ import annotations

import os
import shutil
import stat
import time
import uuid
from contextlib import contextmanager

from ...config import DEFAULT_TEMPLATE_ID, GOLDEN_DIR, GOLDEN_TAP, GUEST_IP, log
from ..._artifacts import sparse_copy
from ..._jailfs import copy_within
from ..._pool import _claim_pool_entry, report_lost
from ...net.netns import _delete_netns
from ...net.netns_pool import acquire_netns
from ...types import BootResult, SandboxRow
from ..common import log_tail, stop_process, timed, wait_for_agent
from . import api
from .jail import FirecrackerJail

ROOTFS = "rootfs.ext4"
PAUSE_MEM = "pause-mem"
PAUSE_VMSTATE = "pause-vmstate"
# A paused VM's complete state, in its jail.
PAUSE_FILES = (ROOTFS, PAUSE_VMSTATE, PAUSE_MEM)
# A template's golden snapshot.
TEMPLATE_FILES = (ROOTFS, "vmstate", "mem")

NS_PREFIX = "fc-"
# Every VM's TAP has the name the golden VM's had: restores reuse its config.
NETWORK_OVERRIDES = [{"iface_id": "eth0", "host_dev_name": GOLDEN_TAP}]


def tap_for_ns(_ns_name: str) -> str:
    return GOLDEN_TAP


def template_snapshot_dir(template_id: str) -> str:
    if template_id == DEFAULT_TEMPLATE_ID:
        return GOLDEN_DIR
    from ..._template_registry import get_template_dir
    return get_template_dir(template_id)


def boot(
    *,
    template_id: str = DEFAULT_TEMPLATE_ID,
    allow_internet_access: bool = True,
    use_pool: bool = True,
    use_pyroute2: bool = True,
    vm_id: str | None = None,
) -> BootResult:
    """Start a jailed VM from ``template_id``'s snapshot.

    On failure everything it created is cleaned up; on success the caller owns the VM.
    """
    vm_id = vm_id or str(uuid.uuid4())
    jail = FirecrackerJail(vm_id)
    snapshot_dir = template_snapshot_dir(template_id)

    timings: dict[str, float] = {}
    t_total = time.monotonic()
    process = None
    pool_entry_dir = None
    ns_name = ""
    rootfs = jail.host_path("/" + ROOTFS)

    try:
        jail.prepare()

        with timed(timings, "copy_rootfs_ms"):
            if use_pool:
                pool_entry_dir = _claim_pool_entry(template_id)
            if pool_entry_dir:
                try:
                    jail.stage_private(f"{pool_entry_dir}/{ROOTFS}", "/" + ROOTFS)
                    # Pre-synced base for the first diff snapshot on pause.
                    jail.stage_private(f"{pool_entry_dir}/{PAUSE_MEM}", "/" + PAUSE_MEM)
                except FileNotFoundError as e:
                    report_lost(pool_entry_dir, [os.path.basename(e.filename or "")])
                    for name in (ROOTFS, PAUSE_MEM):
                        if os.path.lexists(jail.host_path("/" + name)):
                            os.unlink(jail.host_path("/" + name))
                shutil.rmtree(pool_entry_dir, ignore_errors=True)
                pool_entry_dir = None
            if not os.path.exists(rootfs):
                sparse_copy(f"{snapshot_dir}/{ROOTFS}", rootfs)
                jail.give("/" + ROOTFS)
            # The vmstate bakes in the absolute drive path from when the snapshot was
            # created (e.g. /microvms/.golden/rootfs.ext4). Firecracker running inside
            # the jailer chroot resolves absolute symlink targets relative to the chroot
            # root, so a symlink at that path pointing to /rootfs.ext4 makes it find the
            # staged file without requiring the original host path to exist in the chroot.
            drive_in_chroot = os.path.join(jail.root, snapshot_dir.lstrip("/"), ROOTFS)
            os.makedirs(os.path.dirname(drive_in_chroot), exist_ok=True)
            os.symlink("/" + ROOTFS, drive_in_chroot)

        # Pre-built by the netns pool when available.
        with timed(timings, "netns_setup_ms"):
            net = acquire_netns(
                NS_PREFIX, tap_for_ns, allow_internet_access,
                use_pool=use_pool, use_pyroute2=use_pyroute2,
            )
            ns_name = net.ns_name

        with timed(timings, "stage_snapshot_ms"):
            jail.link_shared(f"{snapshot_dir}/vmstate", "/vmstate")
            jail.link_shared(f"{snapshot_dir}/mem", "/mem")

        with timed(timings, "popen_ms"):
            process = jail.spawn(ns_name)

        with timed(timings, "api_snapshot_load_ms"):
            api.load_snapshot(jail.socket_path, "vmstate", "mem", NETWORK_OVERRIDES)
        with timed(timings, "api_drives_ms"):
            api.patch(jail.socket_path, "/drives/rootfs", {"drive_id": "rootfs", "path_on_host": ROOTFS})
        with timed(timings, "api_resume_ms"):
            api.set_vm_state(jail.socket_path, "Resumed")
        with timed(timings, "agent_connect_ms"):
            agent_url = wait_for_agent(ns_name)

        log.debug("[%s] boot %.0f ms: %s", vm_id[:8], (time.monotonic() - t_total) * 1000,
                  " | ".join(f"{k}={v:.1f}" for k, v in timings.items()))
        return BootResult(
            vm_id=vm_id, process=process, vm_dir=jail.root, agent_url=agent_url, guest_ip=GUEST_IP,
            ns_name=ns_name, socket_path=jail.socket_path, chroot_base=jail.base,
            timings=timings, t_total=t_total, metadata={"veth_ip": net.veth_ip},
        )
    except BaseException:
        stop_process(process)
        if pool_entry_dir:
            shutil.rmtree(pool_entry_dir, ignore_errors=True)
        if ns_name:
            _delete_netns(ns_name)
        jail.cleanup()
        raise


def resume(row: SandboxRow) -> BootResult:
    """Restart the jailer for a paused VM and restore its pause snapshot.

    Its network namespace survived the pause; on failure the jail is kept
    (the pause snapshot is still the VM's state).
    """
    jail = FirecrackerJail(row.vm_id)
    jail.reset(keep=PAUSE_FILES)
    timings: dict[str, float] = {}
    t_total = time.monotonic()
    process = None
    try:
        with timed(timings, "popen_ms"):
            process = jail.spawn(row.ns_name)
        with timed(timings, "api_snapshot_load_ms"):
            api.load_snapshot(jail.socket_path, PAUSE_VMSTATE, PAUSE_MEM, NETWORK_OVERRIDES)
        api.patch(jail.socket_path, "/drives/rootfs", {"drive_id": "rootfs", "path_on_host": ROOTFS})
        api.set_vm_state(jail.socket_path, "Resumed")
        with timed(timings, "agent_connect_ms"):
            agent_url = wait_for_agent(row.ns_name)
    except BaseException:
        log.error("Resume failed — firecracker log:\n%s", log_tail(jail.log_path))
        stop_process(process)
        raise
    return BootResult(
        vm_id=row.vm_id, process=process, vm_dir=jail.root, agent_url=agent_url, guest_ip=GUEST_IP,
        ns_name=row.ns_name, socket_path=jail.socket_path, chroot_base=jail.base,
        timings=timings, t_total=t_total,
        # The netns survives pause, and storage deregisters exports by this IP on kill.
        metadata={"veth_ip": row.backend_metadata.get("veth_ip", "")},
    )


@contextmanager
def guest_paused(socket_path: str):
    """Pause the guest for the block; resume it afterwards, whatever happens."""
    api.set_vm_state(socket_path, "Paused")
    try:
        yield
    finally:
        api.set_vm_state(socket_path, "Resumed")


def write_pause_snapshot(jail: FirecrackerJail) -> None:
    """With the guest paused, write its state to ``pause-vmstate``/``pause-mem`` in its jail."""
    pause_mem = jail.host_path("/" + PAUSE_MEM)
    try:
        try:
            have_base = stat.S_ISREG(os.lstat(pause_mem).st_mode)
        except FileNotFoundError:
            have_base = False
        if not have_base:
            # Pool miss at create: build the base now (slower, same result).
            # The VMM has run guest code, so don't trust paths in its jail.
            copy_within(jail.host_path("/mem"), pause_mem, *jail.identity)
        resp = api.put(jail.socket_path, "/snapshot/create", {
            "snapshot_type": "Diff", "snapshot_path": PAUSE_VMSTATE, "mem_file_path": PAUSE_MEM,
        })
        if not api.status_ok(resp):
            log.warning("[%s] diff snapshot failed, taking a full one: %s", jail.vm_id[:8], api.response_body(resp)[:300])
            resp = api.put(jail.socket_path, "/snapshot/create", {
                "snapshot_type": "Full", "snapshot_path": PAUSE_VMSTATE, "mem_file_path": PAUSE_MEM,
            })
    except Exception as exc:
        resp = str(exc)
    if not api.status_ok(resp):
        raise RuntimeError(f"Snapshot create failed: {resp}")


def pause(vm_id: str) -> str:
    """Pause the guest and snapshot it into its jail; it stays paused. Returns the snapshot's directory."""
    jail = FirecrackerJail(vm_id)
    api.set_vm_state(jail.socket_path, "Paused")
    try:
        write_pause_snapshot(jail)
    except BaseException:
        api.set_vm_state(jail.socket_path, "Resumed")
        raise
    return jail.root

"""Cloud-Hypervisor VMs: boot, pause/resume, golden snapshots.

Every VMM, including the one that takes a template's golden snapshot, runs
in a :class:`CloudHypervisorJail`. The guest workload and host network layout
are identical to the Firecracker backend, so the same ``flintd`` image,
init-net.sh, guest IP, and ARP pre-seed work unchanged.

:func:`boot` restores from a per-template golden snapshot (``<template>/snapshot``)
taken right after flintd came up, so sandboxes skip kernel boot. Templates
without a snapshot fall back to a fresh boot. Per-sandbox pause/resume uses
``/api/v1/vm.snapshot`` + ``/api/v1/vm.restore``.

Snapshots are taken inside the jail, so they reference chroot paths
(``/vmlinux``, ``/rootfs.ext4``) and an fd-backed NIC: every VM restores the
same golden files unchanged, with its own disk and a fresh TAP fd.

With ``FLINT_SHARED_ROOTFS_DIR`` set, VMs instead boot fresh from a disk in
the cluster's shared pool on a migration network (the VMM opens its TAP), so
they can later move between hosts live; their :class:`~flint.core.types.Maintenance`
records the disk and network.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid

from ... import config
from ...config import DEFAULT_TEMPLATE_ID, GUEST_IP, GUEST_MAC, log
from ..._artifacts import dig_holes, golden_inputs_current, record_golden_inputs, sparse_copy
from ..._jailfs import copy_out, copy_within
from ..._pool import _claim_pool_entry, report_lost
from ..._wipe import wipe_tree
from ...net.netns import _delete_netns, _setup_netns_pyroute2, _setup_netns_subprocess, open_tap_fd
from ...net.netns_pool import acquire_netns
from ...types import BootResult, Maintenance
from ..common import log_tail, stop_process, timed, wait_for_agent
from . import api
from .jail import JAIL_KERNEL, JAIL_ROOTFS, CloudHypervisorJail

# CH kernel cmdline — like FC's BOOT_ARGS but without `pci=off` (CH uses
# PCI VirtIO on x86_64) and with an explicit root device, since CH does not
# auto-select the first virtio-block device as root.
CH_BOOT_ARGS = (
    "console=ttyS0 reboot=k panic=1 random.trust_cpu=on mitigations=off "
    "nokaslr root=/dev/vda rw init=/etc/init-net.sh"
)

# Id of the guest NIC. Restores hand CH a fresh TAP fd for it.
NET_ID = "net0"
SNAPSHOT_FILES = ("config.json", "state.json", "memory-ranges")
ROOTFS = "rootfs.ext4"

NS_PREFIX = "ch-"


def ns_name_for(vm_id: str) -> str:
    """The namespace name of a VM booted outside the netns pool."""
    return f"{NS_PREFIX}{vm_id[:8]}"


def tap_for_ns(ns_name: str) -> str:
    """TAP name for a ``ch-<id>`` namespace (≤15 chars)."""
    return f"chtap-{ns_name[len(NS_PREFIX):]}"


def snapshot_dir(template_dir: str) -> str:
    return os.path.join(template_dir, "snapshot")


def snapshot_ready(template_dir: str) -> bool:
    directory = snapshot_dir(template_dir)
    return all(os.path.exists(os.path.join(directory, f)) for f in (*SNAPSHOT_FILES, ROOTFS))


def golden_ready(dest_dir: str | None = None) -> bool:
    return os.path.exists(os.path.join(dest_dir or config.CH_GOLDEN_DIR, ROOTFS))


def template_dir(template_id: str) -> str:
    if template_id == DEFAULT_TEMPLATE_ID:
        return config.CH_GOLDEN_DIR
    from ..._template_registry import get_template_dir
    return get_template_dir(template_id, backend_kind="linux-cloud-hypervisor")


def _vm_config() -> dict:
    """``vm.create`` body for a fresh boot, in chroot paths. The NIC is added separately."""
    from ..._limits import ch_disk_limiter
    return {
        "cpus": {"boot_vcpus": config.CH_CPU_COUNT, "max_vcpus": config.CH_CPU_COUNT},
        "memory": {"size": config.CH_MEMORY_BYTES, "shared": True},
        "payload": {"kernel": JAIL_KERNEL, "cmdline": CH_BOOT_ARGS},
        "disks": [{"path": JAIL_ROOTFS, "readonly": False, "direct": bool(config.SHARED_ROOTFS_DIR),
                   "rate_limiter_config": ch_disk_limiter()}],
        "serial": {"mode": "Tty"},
        "console": {"mode": "Off"},
    }


def _boot_fresh(jail: CloudHypervisorJail, ns_name: str, tap_name: str) -> None:
    vm_config = _vm_config()
    if config.SHARED_ROOTFS_DIR:
        # The VMM opens the TAP itself: the migration target must recreate it.
        vm_config["net"] = [{"id": NET_ID, "mac": GUEST_MAC, "tap": tap_name}]
        api.vm_create(jail.socket_path, vm_config)
        api.vm_boot(jail.socket_path)
        return
    api.vm_create(jail.socket_path, vm_config)
    tap_fd = open_tap_fd(ns_name, tap_name)
    try:
        api.vm_add_net(jail.socket_path, {"id": NET_ID, "mac": GUEST_MAC}, tap_fd)
    finally:
        os.close(tap_fd)
    api.vm_boot(jail.socket_path)


def _restore(jail: CloudHypervisorJail, ns_name: str, tap_name: str, jail_snapshot_dir: str) -> None:
    from ..._limits import ch_disk_limiter
    path = jail.host_path(jail_snapshot_dir + "/config.json")
    # Golden config is an immutable hard link shared with other VMs.
    copy_within(path, path, *jail.identity)
    with open(path) as stream:
        vm_config = json.load(stream)
    for disk in vm_config.get("disks", []):
        disk["rate_limiter_config"] = ch_disk_limiter()
    with open(path, "w") as stream:
        json.dump(vm_config, stream)
    if config.SHARED_ROOTFS_DIR:
        api.vm_restore(jail.socket_path, f"file://{jail_snapshot_dir}")
        api.vm_resume(jail.socket_path)
        return
    tap_fd = open_tap_fd(ns_name, tap_name)
    try:
        api.vm_restore(jail.socket_path, f"file://{jail_snapshot_dir}", {NET_ID: tap_fd})
    finally:
        os.close(tap_fd)
    api.vm_resume(jail.socket_path)


# ── Shared root disks and migration networks ───────────────────────────────


def _shared_pool():
    from ..._shared_rootfs import configured_pool
    return configured_pool()


def attach_shared_disk(jail: CloudHypervisorJail, pool, source: str, maintenance: Maintenance, *, verify: bool = False) -> None:
    """Create the VM's disk in the shared pool from ``source`` and bind-mount it as the jail's root disk."""
    disk = pool.create(jail.vm_id, source, jail.identity[0])
    # The marker is outside the chroot, inaccessible to the VMM.
    with open(jail.shared_disk_marker, "x") as marker:
        marker.write(pool.pool_id)
    pool.attach(jail.vm_id, jail.host_path(JAIL_ROOTFS), expected=disk if verify else None)
    maintenance.root_disk = disk


def migration_network(vm_id: str, maintenance: Maintenance):
    from ...net.migration_network import configured_network
    disk = maintenance.root_disk or _shared_pool().descriptor(vm_id)
    return configured_network(vm_id, disk["vni"])


def setup_migration_network(network, vm_id: str, maintenance: Maintenance, allow_internet_access: bool) -> tuple[str, str]:
    """Ask the gateway to route the VM and build its namespace on the migration network. Returns (ns, tap)."""
    from flint.cluster.gateway import gateway_request
    gateway_request("prepare", vm_id, internet=allow_internet_access)
    ns_name, tap_name = network.setup_compute(allow_internet_access)
    maintenance.network = {"vni": network.vni, "gateway": network.gateway_ip,
                           "underlay": network.underlay, "mtu": network.mtu}
    return ns_name, tap_name


def activate_migration_network(network, vm_id: str) -> None:
    from flint.cluster.gateway import gateway_request
    gateway_request("activate", vm_id)
    network.announce()


# ── Lifecycle ──────────────────────────────────────────────────────────────


def _result(jail: CloudHypervisorJail, process, agent_url: str, ns_name: str, tap_name: str, veth_ip: str,
            maintenance: Maintenance, timings: dict, t_total: float) -> BootResult:
    return BootResult(
        vm_id=jail.vm_id, process=process, vm_dir=jail.root, agent_url=agent_url, guest_ip=GUEST_IP,
        ns_name=ns_name, socket_path=jail.socket_path, chroot_base=jail.base,
        timings=timings, t_total=t_total,
        metadata={"tap_name": tap_name, "veth_ip": veth_ip, "maintenance": maintenance.to_json()},
    )


def boot(
    *,
    template_id: str = DEFAULT_TEMPLATE_ID,
    allow_internet_access: bool = True,
    use_pool: bool = True,
    use_pyroute2: bool = True,
    vm_id: str | None = None,
) -> BootResult:
    """Start a jailed cloud-hypervisor VM for ``template_id``.

    Restores the template's golden snapshot when it has one (~100 ms), else
    boots the kernel from scratch (~500 ms).
    """
    vm_id = vm_id or str(uuid.uuid4())
    jail = CloudHypervisorJail(vm_id)
    ns_name = ""  # set once the namespace is acquired

    tdir = template_dir(template_id)
    sdir = snapshot_dir(tdir)
    restore = snapshot_ready(tdir) and not config.SHARED_ROOTFS_DIR
    rootfs_src = os.path.join(sdir if restore else tdir, ROOTFS)
    if not os.path.exists(rootfs_src):
        raise RuntimeError(f"cloud-hypervisor rootfs not found: {rootfs_src}")

    timings: dict[str, float] = {}
    t_total = time.monotonic()
    process = None
    pool_entry_dir = None
    shared = _shared_pool()
    maintenance = Maintenance()
    network = None
    try:
        jail.prepare()
        if shared:
            attach_shared_disk(jail, shared, rootfs_src, maintenance)

        with timed(timings, "copy_rootfs_ms"):
            # Pool entries are copies of the snapshot's disk, so only use them
            # when restoring; a fresh boot starts from the template rootfs.
            if not shared and use_pool and restore:
                pool_entry_dir = _claim_pool_entry(template_id)
            if pool_entry_dir:
                try:
                    jail.stage_private(f"{pool_entry_dir}/{ROOTFS}", JAIL_ROOTFS)
                except FileNotFoundError:
                    report_lost(pool_entry_dir, [ROOTFS])
                shutil.rmtree(pool_entry_dir, ignore_errors=True)
                pool_entry_dir = None
            if not shared and not os.path.exists(jail.host_path(JAIL_ROOTFS)):
                sparse_copy(rootfs_src, jail.host_path(JAIL_ROOTFS))
            jail.give(JAIL_ROOTFS)

        # Pre-built by the netns pool when available.
        with timed(timings, "netns_setup_ms"):
            if shared:
                network = migration_network(vm_id, maintenance)
                ns_name, tap_name = setup_migration_network(network, vm_id, maintenance, allow_internet_access)
                veth_ip = ""
            else:
                net = acquire_netns(
                    NS_PREFIX, tap_for_ns, allow_internet_access,
                    use_pool=use_pool, use_pyroute2=use_pyroute2,
                )
                ns_name, tap_name, veth_ip = net.ns_name, net.tap_name, net.veth_ip

        with timed(timings, "popen_ms"):
            process = jail.spawn(ns_name)

        if restore:
            with timed(timings, "stage_snapshot_ms"):
                os.mkdir(jail.host_path("/restore"))
                for name in SNAPSHOT_FILES:
                    jail.link_shared(os.path.join(sdir, name), f"/restore/{name}")
            with timed(timings, "api_vm_restore_ms"):
                _restore(jail, ns_name, tap_name, "/restore")
            shutil.rmtree(jail.host_path("/restore"), ignore_errors=True)
        else:
            with timed(timings, "api_vm_boot_ms"):
                _boot_fresh(jail, ns_name, tap_name)

        if network is not None:
            activate_migration_network(network, vm_id)
        with timed(timings, "agent_connect_ms"):
            agent_url = wait_for_agent(ns_name)

        log.debug("[%s] ch %s %.0f ms: %s", vm_id[:8], "restore" if restore else "boot",
                  (time.monotonic() - t_total) * 1000, " | ".join(f"{k}={v:.1f}" for k, v in timings.items()))
        return _result(jail, process, agent_url, ns_name, tap_name, veth_ip, maintenance, timings, t_total)
    except Exception:
        tail = log_tail(jail.log_path)
        if tail:
            log.error("ch boot failed — cloud-hypervisor log:\n%s", tail)
        stop_process(process)
        if pool_entry_dir:
            shutil.rmtree(pool_entry_dir, ignore_errors=True)
        if ns_name:
            _delete_netns(ns_name)
        jail.cleanup()
        raise


def shutdown(socket_path: str) -> None:
    """Ask the VMM to exit cleanly (best effort; it is killed after anyway)."""
    if socket_path and os.path.exists(socket_path):
        try:
            api.vmm_shutdown(socket_path)
        except Exception as exc:
            log.debug("vmm.shutdown via %s failed: %s", socket_path, exc)


def pause(vm_id: str) -> str:
    """Pause + snapshot a running VM to ``/pause`` in its jail; it stays paused. Returns the host path."""
    jail = CloudHypervisorJail(vm_id)
    pause_dir = jail.host_path("/pause")
    # CH refuses to overwrite an existing snapshot, and a VM that was paused
    # before still has one here. Write a fresh one and swap it in; restore
    # copies memory, so the running VM no longer needs the old files.
    staging = jail.host_path("/pause.tmp")
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging)
    jail.give("/pause.tmp")
    api.vm_pause(jail.socket_path)
    try:
        api.vm_snapshot(jail.socket_path, "file:///pause.tmp")
    except Exception:
        wipe_tree(staging, vm_id)
        try:
            api.vm_resume(jail.socket_path)
        except Exception:
            log.exception("[%s] resuming after a failed pause snapshot failed", vm_id[:8])
        raise
    wipe_tree(pause_dir, vm_id)  # the previous pause's memory
    os.rename(staging, pause_dir)
    return pause_dir


def resume(
    vm_id: str,
    *,
    ns_name: str,
    tap_name: str,
    use_pyroute2: bool = True,
    allow_internet_access: bool = True,
    veth_ip: str = "",
    maintenance: Maintenance | None = None,
) -> BootResult:
    """Restart the jailed VMM and restore from the snapshot in its jail's ``/pause``."""
    jail = CloudHypervisorJail(vm_id)
    maintenance = maintenance or Maintenance()
    network = migration_network(vm_id, maintenance) if config.SHARED_ROOTFS_DIR else None

    # The netns survives pause; it's gone only after a host reboot.
    if not os.path.exists(f"/var/run/netns/{ns_name}"):
        if network is not None:
            ns_name, tap_name = setup_migration_network(network, vm_id, maintenance, allow_internet_access)
        else:
            setup = _setup_netns_pyroute2 if use_pyroute2 else _setup_netns_subprocess
            veth_ip = setup(ns_name, tap_name, internet=allow_internet_access)

    timings: dict[str, float] = {}
    t_total = time.monotonic()
    process = None
    try:
        with timed(timings, "popen_ms"):
            process = jail.spawn(ns_name)
        with timed(timings, "api_vm_restore_ms"):
            _restore(jail, ns_name, tap_name, "/pause")
        if network is not None:
            activate_migration_network(network, vm_id)
        with timed(timings, "agent_connect_ms"):
            agent_url = wait_for_agent(ns_name)
    except Exception:
        tail = log_tail(jail.log_path)
        if tail:
            log.error("ch resume failed — cloud-hypervisor log:\n%s", tail)
        stop_process(process)
        raise
    return _result(jail, process, agent_url, ns_name, tap_name, veth_ip, maintenance, timings, t_total)


# ── Golden snapshots ───────────────────────────────────────────────────────


def create_golden_snapshot(template_dir: str) -> None:
    """Boot the template once (jailed), wait for flintd, and snapshot it to ``<template_dir>/snapshot``.

    The template's image is tenant-supplied, so this boot is as confined as a
    sandbox. The snapshot keeps the exact disk the golden VM ran on, since
    guest page cache in the memory image must match it.
    """
    sdir = snapshot_dir(template_dir)
    staging = f"{sdir}.tmp"
    shutil.rmtree(staging, ignore_errors=True)

    vm_id = f"golden-{uuid.uuid4()}"
    jail = CloudHypervisorJail(vm_id)
    ns_name = ns_name_for(vm_id[len("golden-"):])
    tap_name = tap_for_ns(ns_name)
    from ..._build_job import register_vm
    register_vm(vm_id, ns_name, "linux-cloud-hypervisor")
    process = None
    t0 = time.monotonic()
    try:
        jail.prepare()
        sparse_copy(os.path.join(template_dir, ROOTFS), jail.host_path(JAIL_ROOTFS))
        jail.give(JAIL_ROOTFS)
        _setup_netns_pyroute2(ns_name, tap_name, internet=False)
        process = jail.spawn(ns_name)
        _boot_fresh(jail, ns_name, tap_name)
        wait_for_agent(ns_name)
        os.mkdir(jail.host_path("/snapshot"))
        jail.give("/snapshot")
        api.vm_pause(jail.socket_path)
        api.vm_snapshot(jail.socket_path, "file:///snapshot")

        # The template image ran in this VMM: copy out without following links.
        os.makedirs(staging)
        for name in SNAPSHOT_FILES:
            copy_out(jail.host_path(f"/snapshot/{name}"), os.path.join(staging, name))
        copy_out(jail.host_path(JAIL_ROOTFS), os.path.join(staging, ROOTFS))
    except Exception:
        tail = log_tail(jail.log_path)
        if tail:
            log.error("ch golden snapshot failed — cloud-hypervisor log:\n%s", tail)
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        shutdown(jail.socket_path)
        stop_process(process)
        _delete_netns(ns_name)
        jail.cleanup()

    dig_holes(os.path.join(staging, "memory-ranges"))
    shutil.rmtree(sdir, ignore_errors=True)
    os.rename(staging, sdir)
    log.info("CH golden snapshot ready in %s (%.0f ms)", sdir, (time.monotonic() - t0) * 1000)


def _machine() -> str:
    """The VM shape baked into a CH snapshot; restores keep it, so changing it means a rebuild."""
    return f"shared=on cpus={config.CH_CPU_COUNT} memory={config.CH_MEMORY_BYTES} cmdline={CH_BOOT_ARGS}"


def create_golden(dest_dir: str | None = None, source_rootfs: str | None = None) -> None:
    """Install the default CH template rootfs and its golden snapshot."""
    dest_dir = dest_dir or config.CH_GOLDEN_DIR
    source_rootfs = source_rootfs or config.SOURCE_ROOTFS
    try:
        source_size = os.path.getsize(source_rootfs)
    except FileNotFoundError as exc:
        raise RuntimeError(f"source rootfs not found: {source_rootfs}") from exc
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, ROOTFS)
    if not os.path.exists(dest) or not golden_inputs_current(dest_dir, _machine()):
        shutil.rmtree(snapshot_dir(dest_dir), ignore_errors=True)
        sparse_copy(source_rootfs, dest)
        log.info("CH golden rootfs installed: %s (%d bytes)", dest, source_size)
    if not snapshot_ready(dest_dir):
        create_golden_snapshot(dest_dir)
    record_golden_inputs(dest_dir, _machine())

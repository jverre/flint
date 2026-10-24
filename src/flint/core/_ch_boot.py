"""Cloud-Hypervisor boot + snapshot pipeline.

Mirrors the Firecracker :mod:`_boot` / :mod:`_snapshot` modules but targets the
cloud-hypervisor REST API. Every VMM, including the one that takes a template's
golden snapshot, runs jailed (see :mod:`_ch_jail`). The guest workload and host
network layout are identical to the FC backend, so the same ``flintd`` image,
init-net.sh, guest IP, and ARP pre-seed work unchanged.

create() restores from a per-template golden snapshot (``<template>/snapshot``)
taken right after flintd came up, so sandboxes skip kernel boot. Templates
without a snapshot fall back to a fresh boot. Per-sandbox pause/resume uses
``/api/v1/vm.snapshot`` + ``/api/v1/vm.restore``.

Snapshots are taken inside the jail, so they reference chroot paths
(``/vmlinux``, ``/rootfs.ext4``) and an fd-backed NIC: every VM restores the
same golden files unchanged, with its own disk and a fresh TAP fd.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field

from . import _cloud_hypervisor as _ch
from ._boot import _RecoveredProcess as _RecoveredChProcess, _timed  # noqa: F401 (re-exported)
from ._artifacts import dig_holes, golden_inputs_current, link_into, record_golden_inputs, sparse_copy
from ._ch_jail import JAIL_KERNEL, JAIL_ROOTFS, ChJail
from ._jailfs import copy_out
from ._firecracker import _wait_for_agent
from ._netns import (
    _delete_netns,
    _setup_netns_pyroute2,
    _setup_netns_subprocess,
    open_tap_fd,
)
from ._netns_pool import acquire_netns
from ._wipe import wipe_tree
from ._pool import _claim_pool_entry
from .config import (
    CH_CPU_COUNT,
    CH_MEMORY_BYTES,
    CH_GOLDEN_DIR,
    CH_UID,
    DEFAULT_TEMPLATE_ID,
    GUEST_MAC,
    SOURCE_ROOTFS,
    log,
)

# CH kernel cmdline — like FC's BOOT_ARGS but without `pci=off` (CH uses
# PCI VirtIO on x86_64) and with an explicit root device, since CH does not
# auto-select the first virtio-block device as root.
CH_BOOT_ARGS = (
    "console=ttyS0 reboot=k panic=1 random.trust_cpu=on mitigations=off "
    "nokaslr root=/dev/vda rw init=/etc/init-net.sh"
)

# Id of the guest NIC. Restores hand CH a fresh TAP fd for it.
NET_ID = "net0"
_SNAPSHOT_FILES = ("config.json", "state.json", "memory-ranges")


def _ch_ns_name(vm_id: str) -> str:
    return f"ch-{vm_id[:8]}"


def _ch_tap_name(vm_id: str) -> str:
    return f"chtap-{vm_id[:8]}"


def ch_tap_for_ns(ns_name: str) -> str:
    """TAP name for a pooled ``ch-<id>`` namespace (≤15 chars)."""
    return f"chtap-{ns_name[len('ch-'):]}"


@dataclass
class ChBootResult:
    vm_id: str
    vm_dir: str          # chroot root
    chroot_base: str
    socket_path: str     # host path
    ns_name: str
    process: subprocess.Popen
    agent_url: str
    tap_name: str
    veth_ip: str = ""
    maintenance: dict = field(default_factory=dict)
    timings: dict[str, float] = field(default_factory=dict)
    t_total: float = 0.0


def _build_vm_config() -> dict:
    """``vm.create`` body for a fresh boot, in chroot paths. The NIC is added via ``vm.add-net``."""
    return {
        "cpus": {"boot_vcpus": CH_CPU_COUNT, "max_vcpus": CH_CPU_COUNT},
        "memory": {"size": CH_MEMORY_BYTES, "shared": True},
        "payload": {"kernel": JAIL_KERNEL, "cmdline": CH_BOOT_ARGS},
        "disks": [{"path": JAIL_ROOTFS, "readonly": False, "direct": bool(os.environ.get("FLINT_SHARED_ROOTFS_DIR"))}],
        "serial": {"mode": "Tty"},
        "console": {"mode": "Off"},
    }


def _boot_fresh(jail: ChJail, ns_name: str, tap_name: str) -> None:
    vm_config = _build_vm_config()
    if os.environ.get("FLINT_SHARED_ROOTFS_DIR"):
        vm_config["net"] = [{"id": NET_ID, "mac": GUEST_MAC, "tap": tap_name}]
        _ch.vm_create(jail.socket_path, vm_config)
        _ch.vm_boot(jail.socket_path)
        return
    _ch.vm_create(jail.socket_path, vm_config)
    tap_fd = open_tap_fd(ns_name, tap_name)
    try:
        _ch.vm_add_net(jail.socket_path, {"id": NET_ID, "mac": GUEST_MAC}, tap_fd)
    finally:
        os.close(tap_fd)
    _ch.vm_boot(jail.socket_path)


def _restore(jail: ChJail, ns_name: str, tap_name: str, jail_snapshot_dir: str) -> None:
    if os.environ.get("FLINT_SHARED_ROOTFS_DIR"):
        _ch.vm_restore(jail.socket_path, f"file://{jail_snapshot_dir}")
        _ch.vm_resume(jail.socket_path)
        return
    tap_fd = open_tap_fd(ns_name, tap_name)
    try:
        _ch.vm_restore(jail.socket_path, f"file://{jail_snapshot_dir}", {NET_ID: tap_fd})
    finally:
        os.close(tap_fd)
    _ch.vm_resume(jail.socket_path)


def _log_tail(path: str, nbytes: int = 2000) -> str:
    """Return the last ``nbytes`` of ``path`` without loading the whole file."""
    try:
        with open(path, "rb") as f:
            try:
                f.seek(-nbytes, os.SEEK_END)
            except OSError:
                f.seek(0)
            return f.read().decode(errors="replace")
    except OSError:
        return ""


def _template_dir(template_id: str) -> str:
    from ._template_registry import get_template_dir

    if template_id == DEFAULT_TEMPLATE_ID:
        return CH_GOLDEN_DIR
    return get_template_dir(template_id, backend_kind="linux-cloud-hypervisor")


def ch_boot(
    *,
    template_id: str = DEFAULT_TEMPLATE_ID,
    allow_internet_access: bool = True,
    use_pool: bool = True,
    use_pyroute2: bool = True,
    vm_id: str | None = None,
) -> ChBootResult:
    """Start a jailed cloud-hypervisor VM for ``template_id``.

    Restores the template's golden snapshot when it has one (~100 ms), else
    boots the kernel from scratch (~500 ms).
    """
    vm_id = vm_id or str(uuid.uuid4())
    jail = ChJail(vm_id)
    ns_name = ""  # set once the namespace is acquired
    sid = vm_id[:8]

    template_dir = _template_dir(template_id)
    snapshot_dir = _snapshot_dir(template_dir)
    restore = ch_snapshot_ready(template_dir) and not os.environ.get("FLINT_SHARED_ROOTFS_DIR")
    rootfs_src = os.path.join(snapshot_dir if restore else template_dir, "rootfs.ext4")
    if not os.path.exists(rootfs_src):
        raise RuntimeError(f"cloud-hypervisor rootfs not found: {rootfs_src}")

    timings: dict[str, float] = {}
    t_total = time.monotonic()
    process: subprocess.Popen | None = None
    pool_entry_dir = None

    from ._shared_rootfs import configured_pool
    pool = configured_pool()
    maintenance = {}
    try:
        jail.prepare()
        if pool:
            maintenance["root_disk"] = pool.create(vm_id, rootfs_src, CH_UID)
            # Marker is outside the chroot, inaccessible to the VMM.
            with open(f"{jail.base}/shared-rootfs", "x") as marker:
                marker.write(pool.pool_id)
            pool.attach(vm_id, jail.host_path(JAIL_ROOTFS))

        with _timed(timings, "copy_rootfs_ms"):
            # Pool entries are copies of the snapshot's disk, so only use them
            # when restoring; a fresh boot starts from the template rootfs.
            if pool:
                pass
            elif use_pool and restore:
                pool_entry_dir = _claim_pool_entry(template_id)
            if pool_entry_dir:
                link_into(f"{pool_entry_dir}/rootfs.ext4", jail.host_path(JAIL_ROOTFS))
                shutil.rmtree(pool_entry_dir, ignore_errors=True)
                pool_entry_dir = None
            elif not pool:
                sparse_copy(rootfs_src, jail.host_path(JAIL_ROOTFS))
            jail.give(JAIL_ROOTFS)

        # Pre-built by the netns pool when available.
        with _timed(timings, "netns_setup_ms"):
            if pool:
                from ._migration_network import configured_network
                from flint.cluster.gateway import gateway_request
                network = configured_network(vm_id, maintenance["root_disk"]["vni"])
                gateway_request("prepare", vm_id, internet=allow_internet_access)
                ns_name, tap_name = network.setup_compute(allow_internet_access)
                veth_ip = ""
                maintenance["network"] = {"vni": network.vni, "gateway": network.gateway_ip,
                                           "underlay": network.underlay, "mtu": network.mtu}
            else:
                net = acquire_netns(
                    "ch-", ch_tap_for_ns, allow_internet_access,
                    use_pool=use_pool, use_pyroute2=use_pyroute2,
                )
                ns_name, tap_name, veth_ip = net.ns_name, net.tap_name, net.veth_ip

        with _timed(timings, "popen_ms"):
            process = jail.spawn(ns_name)

        if restore:
            with _timed(timings, "stage_snapshot_ms"):
                os.mkdir(jail.host_path("/restore"))
                for name in _SNAPSHOT_FILES:
                    jail.link_shared(os.path.join(snapshot_dir, name), f"/restore/{name}")
            with _timed(timings, "api_vm_restore_ms"):
                _restore(jail, ns_name, tap_name, "/restore")
            shutil.rmtree(jail.host_path("/restore"), ignore_errors=True)
        else:
            with _timed(timings, "api_vm_boot_ms"):
                _boot_fresh(jail, ns_name, tap_name)

        if pool:
            gateway_request("activate", vm_id)
            network.announce()
        with _timed(timings, "agent_connect_ms"):
            agent_url = _wait_for_agent(ns_name)

        total_ms = (time.monotonic() - t_total) * 1000
        parts = " | ".join(f"{k}={v:.1f}" for k, v in timings.items())
        log.debug("[%s] ch %s %.0f ms: %s", sid, "restore" if restore else "boot", total_ms, parts)

        return ChBootResult(
            vm_id=vm_id,
            maintenance=maintenance,
            vm_dir=jail.root,
            chroot_base=jail.base,
            socket_path=jail.socket_path,
            ns_name=ns_name,
            process=process,
            agent_url=agent_url,
            tap_name=tap_name,
            veth_ip=veth_ip,
            timings=timings,
            t_total=t_total,
        )

    except Exception:
        tail = _log_tail(jail.log_path)
        if tail:
            log.error("ch boot failed — cloud-hypervisor log:\n%s", tail)
        if process is not None:
            try:
                process.kill()
                process.wait(timeout=2)
            except Exception:
                pass
        if pool_entry_dir:
            shutil.rmtree(pool_entry_dir, ignore_errors=True)
        if ns_name:
            _delete_netns(ns_name)
        jail.cleanup()
        raise


def ch_teardown(process, ns_name: str, vm_id: str, socket_path: str = "") -> None:
    """Best-effort VM teardown: graceful shutdown → SIGKILL → remove netns, jail and cgroup."""
    if socket_path and os.path.exists(socket_path):
        try:
            _ch.vmm_shutdown(socket_path)
        except Exception:
            pass
    if process is not None:
        try:
            process.kill()
        except Exception:
            pass
        try:
            process.wait(timeout=2)
        except Exception:
            pass
    _delete_netns(ns_name)
    ChJail(vm_id).cleanup()


def ch_pause_vm(vm_id: str) -> str:
    """Pause + snapshot a running CH VM to ``/pause`` in its jail. Returns the host path."""
    jail = ChJail(vm_id)
    pause_dir = jail.host_path("/pause")
    # CH refuses to overwrite an existing snapshot, and a VM that was paused
    # before still has one here. Write a fresh one and swap it in; restore
    # copies memory, so the running VM no longer needs the old files.
    staging = jail.host_path("/pause.tmp")
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging)
    jail.give("/pause.tmp")
    _ch.vm_pause(jail.socket_path)
    try:
        _ch.vm_snapshot(jail.socket_path, "file:///pause.tmp")
    except Exception:
        wipe_tree(staging, vm_id)
        try:
            _ch.vm_resume(jail.socket_path)
        except Exception:
            pass
        raise
    wipe_tree(pause_dir, vm_id)  # the previous pause's memory
    os.rename(staging, pause_dir)
    return pause_dir


def ch_resume_from_pause(
    vm_id: str,
    *,
    ns_name: str,
    tap_name: str,
    use_pyroute2: bool = True,
    allow_internet_access: bool = True,
    veth_ip: str = "",
    maintenance: dict | None = None,
) -> ChBootResult:
    """Restart the jailed VMM and restore from the sandbox's pause snapshot."""
    jail = ChJail(vm_id)

    maintenance = maintenance or {}
    shared = bool(os.environ.get("FLINT_SHARED_ROOTFS_DIR"))
    if shared:
        from ._shared_rootfs import configured_pool
        from ._migration_network import configured_network
        from flint.cluster.gateway import gateway_request
        disk = maintenance.get("root_disk") or configured_pool().descriptor(vm_id)
        network = configured_network(vm_id,disk["vni"])

    # The netns survives pause; it's gone only after a host reboot.
    if not os.path.exists(f"/var/run/netns/{ns_name}"):
        if shared:
            gateway_request("prepare", vm_id, internet=allow_internet_access)
            ns_name,tap_name = network.setup_compute(allow_internet_access)
        else:
            setup = _setup_netns_pyroute2 if use_pyroute2 else _setup_netns_subprocess
            veth_ip = setup(ns_name, tap_name, internet=allow_internet_access)

    timings: dict[str, float] = {}
    t_total = time.monotonic()
    process = None
    try:
        with _timed(timings, "popen_ms"):
            process = jail.spawn(ns_name)
        with _timed(timings, "api_vm_restore_ms"):
            _restore(jail, ns_name, tap_name, "/pause")
        if shared:
            gateway_request("activate",vm_id)
            network.announce()
        with _timed(timings, "agent_connect_ms"):
            agent_url = _wait_for_agent(ns_name)
    except Exception:
        tail = _log_tail(jail.log_path)
        if tail:
            log.error("ch resume failed — cloud-hypervisor log:\n%s", tail)
        if process is not None:
            try:
                process.kill()
                process.wait(timeout=2)
            except Exception:
                pass
        raise

    return ChBootResult(
        vm_id=vm_id,
        maintenance=maintenance,
        vm_dir=jail.root,
        chroot_base=jail.base,
        socket_path=jail.socket_path,
        ns_name=ns_name,
        process=process,
        agent_url=agent_url,
        tap_name=tap_name,
        veth_ip=veth_ip,
        timings=timings,
        t_total=t_total,
    )


def _snapshot_dir(template_dir: str) -> str:
    return os.path.join(template_dir, "snapshot")


def ch_snapshot_ready(template_dir: str) -> bool:
    snapshot_dir = _snapshot_dir(template_dir)
    return all(os.path.exists(os.path.join(snapshot_dir, f)) for f in (*_SNAPSHOT_FILES, "rootfs.ext4"))


def ch_create_golden_snapshot(template_dir: str) -> None:
    """Boot the template once (jailed), wait for flintd, and snapshot it to ``<template_dir>/snapshot``.

    The template's image is tenant-supplied, so this boot is as confined as a
    sandbox. The snapshot keeps the exact disk the golden VM ran on, since
    guest page cache in the memory image must match it.
    """
    snapshot_dir = _snapshot_dir(template_dir)
    staging = f"{snapshot_dir}.tmp"
    shutil.rmtree(staging, ignore_errors=True)

    vm_id = f"golden-{uuid.uuid4()}"
    jail = ChJail(vm_id)
    ns_name = _ch_ns_name(vm_id[len("golden-"):])
    tap_name = ch_tap_for_ns(ns_name)
    process = None
    t0 = time.monotonic()
    try:
        jail.prepare()
        sparse_copy(os.path.join(template_dir, "rootfs.ext4"), jail.host_path(JAIL_ROOTFS))
        jail.give(JAIL_ROOTFS)
        _setup_netns_pyroute2(ns_name, tap_name, internet=False)
        process = jail.spawn(ns_name)
        _boot_fresh(jail, ns_name, tap_name)
        _wait_for_agent(ns_name)
        os.mkdir(jail.host_path("/snapshot"))
        jail.give("/snapshot")
        _ch.vm_pause(jail.socket_path)
        _ch.vm_snapshot(jail.socket_path, "file:///snapshot")

        # The template image ran in this VMM: copy out without following links.
        os.makedirs(staging)
        for name in _SNAPSHOT_FILES:
            copy_out(jail.host_path(f"/snapshot/{name}"), os.path.join(staging, name))
        copy_out(jail.host_path(JAIL_ROOTFS), os.path.join(staging, "rootfs.ext4"))
    except Exception:
        tail = _log_tail(jail.log_path)
        if tail:
            log.error("ch golden snapshot failed — cloud-hypervisor log:\n%s", tail)
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        ch_teardown(process, ns_name, vm_id, jail.socket_path)

    dig_holes(os.path.join(staging, "memory-ranges"))
    shutil.rmtree(snapshot_dir, ignore_errors=True)
    os.rename(staging, snapshot_dir)
    log.info("CH golden snapshot ready in %s (%.0f ms)", snapshot_dir, (time.monotonic() - t0) * 1000)


def _machine() -> str:
    """The VM shape baked into a CH snapshot; restores keep it, so changing it means a rebuild."""
    return f"shared=on cpus={CH_CPU_COUNT} memory={CH_MEMORY_BYTES} cmdline={CH_BOOT_ARGS}"


def ch_create_golden(dest_dir: str = CH_GOLDEN_DIR, source_rootfs: str = SOURCE_ROOTFS) -> None:
    """Install the default CH template rootfs and its golden snapshot."""
    try:
        source_size = os.path.getsize(source_rootfs)
    except FileNotFoundError as exc:
        raise RuntimeError(f"source rootfs not found: {source_rootfs}") from exc
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, "rootfs.ext4")
    if not os.path.exists(dest) or not golden_inputs_current(dest_dir, _machine()):
        shutil.rmtree(_snapshot_dir(dest_dir), ignore_errors=True)
        sparse_copy(source_rootfs, dest)
        log.info("CH golden rootfs installed: %s (%d bytes)", dest, source_size)
    if not ch_snapshot_ready(dest_dir):
        ch_create_golden_snapshot(dest_dir)
    record_golden_inputs(dest_dir, _machine())


def ch_golden_ready(dest_dir: str = CH_GOLDEN_DIR) -> bool:
    return os.path.exists(os.path.join(dest_dir, "rootfs.ext4"))

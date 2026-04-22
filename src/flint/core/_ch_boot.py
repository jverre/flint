"""Cloud-Hypervisor boot + snapshot pipeline.

Mirrors the Firecracker :mod:`_boot` / :mod:`_snapshot` modules but targets the
cloud-hypervisor REST API. CH has no jailer of its own, so we isolate by running
the process inside a per-VM netns and (when available) a dedicated systemd slice
with ``--seccomp true``. The guest workload and host network layout are
identical to the FC backend, so the same ``flintd`` image, init-net.sh, guest
IP, and ARP pre-seed work unchanged.

Fresh-boot path (no snapshot/restore) is used for per-sandbox create() to keep
the pipeline simple and robust. Per-sandbox pause/resume uses
``/api/v1/vm.snapshot`` + ``/api/v1/vm.restore``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field

from . import _cloud_hypervisor as _ch
from ._boot import _RecoveredProcess as _RecoveredChProcess, _timed
from ._firecracker import _wait_for_agent
from ._netns import (
    _delete_netns,
    _popen_in_ns,
    _setup_netns_pyroute2,
    _setup_netns_subprocess,
)
from .config import (
    CH_BINARY,
    CH_CPU_COUNT,
    CH_MEMORY_BYTES,
    CH_GOLDEN_DIR,
    DATA_DIR,
    DEFAULT_TEMPLATE_ID,
    GUEST_MAC,
    KERNEL_PATH,
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


def _ch_ns_name(vm_id: str) -> str:
    return f"ch-{vm_id[:8]}"


def _ch_tap_name(vm_id: str) -> str:
    return f"chtap-{vm_id[:8]}"


@dataclass
class ChBootResult:
    vm_id: str
    vm_dir: str
    socket_path: str
    ns_name: str
    process: subprocess.Popen
    agent_url: str
    tap_name: str
    veth_ip: str = ""
    timings: dict[str, float] = field(default_factory=dict)
    t_total: float = 0.0


def _build_vm_config(rootfs_path: str, tap_name: str) -> dict:
    """Build the JSON config body for ``PUT /api/v1/vm.create``."""
    return {
        "cpus": {"boot_vcpus": CH_CPU_COUNT, "max_vcpus": CH_CPU_COUNT},
        "memory": {"size": CH_MEMORY_BYTES},
        "payload": {
            "kernel": KERNEL_PATH,
            "cmdline": CH_BOOT_ARGS,
        },
        "disks": [{"path": rootfs_path, "readonly": False}],
        "net": [{"tap": tap_name, "mac": GUEST_MAC}],
        "serial": {"mode": "Tty"},
        "console": {"mode": "Off"},
    }


def _popen_cloud_hypervisor(
    ns_name: str,
    socket_path: str,
    log_path: str,
) -> subprocess.Popen:
    """Spawn cloud-hypervisor inside ``ns_name`` with its API socket bound."""
    # cloud-hypervisor honours SIGUSR1/SIGTERM for graceful shutdown; we use
    # --seccomp true + --log-file for hardening and observability.
    cmd = [
        CH_BINARY,
        "--api-socket", f"path={socket_path}",
        "--seccomp", "true",
    ]
    log_fd = open(log_path, "w")
    return _popen_in_ns(
        ns_name,
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=log_fd,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


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


def ch_boot_fresh(
    *,
    template_id: str = DEFAULT_TEMPLATE_ID,
    allow_internet_access: bool = True,
    use_pyroute2: bool = True,
) -> ChBootResult:
    """Fresh-boot a cloud-hypervisor VM from a template rootfs.

    No snapshot/restore; each sandbox boots from scratch via vm.create +
    vm.boot. That's ~1-2s (kernel init + flintd start) versus ~200ms for
    the FC snapshot-restore path, but correct behaviour first.
    """
    from ._template_registry import get_template_dir

    vm_id = str(uuid.uuid4())
    ns_name = _ch_ns_name(vm_id)
    tap_name = _ch_tap_name(vm_id)
    sid = vm_id[:8]

    if template_id == DEFAULT_TEMPLATE_ID:
        template_dir = CH_GOLDEN_DIR
    else:
        template_dir = get_template_dir(template_id, backend_kind="linux-cloud-hypervisor")

    rootfs_src = os.path.join(template_dir, "rootfs.ext4")
    if not os.path.exists(rootfs_src):
        raise RuntimeError(f"cloud-hypervisor rootfs not found: {rootfs_src}")

    vm_dir = f"{DATA_DIR}/ch-{vm_id}"
    socket_path = f"{vm_dir}/ch.sock"
    rootfs_path = f"{vm_dir}/rootfs.ext4"
    log_path = f"{vm_dir}/cloud-hypervisor.log"

    timings: dict[str, float] = {}
    t_total = time.monotonic()
    process: subprocess.Popen | None = None

    try:
        os.makedirs(vm_dir, exist_ok=True)

        with _timed(timings, "copy_rootfs_ms"):
            subprocess.run(
                ["cp", "--reflink=auto", rootfs_src, rootfs_path], check=True
            )

        with _timed(timings, "netns_setup_ms"):
            if use_pyroute2:
                veth_ip = _setup_netns_pyroute2(
                    ns_name, tap_name, internet=allow_internet_access
                )
            else:
                veth_ip = _setup_netns_subprocess(
                    ns_name, tap_name, internet=allow_internet_access
                )

        with _timed(timings, "popen_ms"):
            process = _popen_cloud_hypervisor(ns_name, socket_path, log_path)

        with _timed(timings, "wait_api_ready_ms"):
            _ch.wait_for_api_socket(socket_path)

        with _timed(timings, "api_vm_create_ms"):
            _ch.vm_create(socket_path, _build_vm_config(rootfs_path, tap_name))

        with _timed(timings, "api_vm_boot_ms"):
            _ch.vm_boot(socket_path)

        with _timed(timings, "agent_connect_ms"):
            agent_url = _wait_for_agent(ns_name)

        total_ms = (time.monotonic() - t_total) * 1000
        parts = " | ".join(f"{k}={v:.1f}" for k, v in timings.items())
        log.debug("[%s] ch boot %.0f ms: %s", sid, total_ms, parts)

        return ChBootResult(
            vm_id=vm_id,
            vm_dir=vm_dir,
            socket_path=socket_path,
            ns_name=ns_name,
            process=process,
            agent_url=agent_url,
            tap_name=tap_name,
            veth_ip=veth_ip,
            timings=timings,
            t_total=t_total,
        )

    except Exception:
        tail = _log_tail(log_path)
        if tail:
            log.error("ch boot failed — cloud-hypervisor log:\n%s", tail)
        if process is not None:
            try:
                process.kill()
                process.wait(timeout=2)
            except Exception:
                pass
        _delete_netns(ns_name)
        shutil.rmtree(vm_dir, ignore_errors=True)
        raise


def ch_teardown(
    process, ns_name: str, vm_dir: str, socket_path: str = ""
) -> None:
    """Best-effort VM teardown: graceful shutdown → SIGKILL → remove netns+dir."""
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
    if vm_dir:
        shutil.rmtree(vm_dir, ignore_errors=True)


def ch_pause_vm(socket_path: str, vm_dir: str) -> str:
    """Pause + snapshot a running CH VM to ``<vm_dir>/pause/``. Returns the pause dir."""
    pause_dir = os.path.join(vm_dir, "pause")
    os.makedirs(pause_dir, exist_ok=True)
    _ch.vm_pause(socket_path)
    try:
        _ch.vm_snapshot(socket_path, f"file://{pause_dir}")
    except Exception:
        try:
            _ch.vm_resume(socket_path)
        except Exception:
            pass
        raise
    return pause_dir


def ch_resume_from_pause(
    vm_id: str,
    vm_dir: str,
    pause_dir: str,
    *,
    ns_name: str,
    tap_name: str,
    use_pyroute2: bool = True,
    allow_internet_access: bool = True,
) -> ChBootResult:
    """Restart cloud-hypervisor and restore from a per-sandbox pause snapshot."""
    socket_path = os.path.join(vm_dir, "ch.sock")
    log_path = os.path.join(vm_dir, "cloud-hypervisor.log")

    # Netns may still exist from the running-state teardown (we kill only the
    # CH process on pause); ensure it's present.
    if not os.path.exists(f"/var/run/netns/{ns_name}"):
        if use_pyroute2:
            _setup_netns_pyroute2(ns_name, tap_name, internet=allow_internet_access)
        else:
            _setup_netns_subprocess(ns_name, tap_name, internet=allow_internet_access)

    try:
        os.unlink(socket_path)
    except FileNotFoundError:
        pass

    process = _popen_cloud_hypervisor(ns_name, socket_path, log_path)

    timings: dict[str, float] = {}
    t_total = time.monotonic()
    try:
        with _timed(timings, "wait_api_ready_ms"):
            _ch.wait_for_api_socket(socket_path)
        with _timed(timings, "api_vm_restore_ms"):
            _ch.vm_restore(socket_path, f"file://{pause_dir}")
        with _timed(timings, "api_vm_resume_ms"):
            _ch.vm_resume(socket_path)
        with _timed(timings, "agent_connect_ms"):
            agent_url = _wait_for_agent(ns_name)
    except Exception:
        tail = _log_tail(log_path)
        if tail:
            log.error("ch resume failed — cloud-hypervisor log:\n%s", tail)
        try:
            process.kill()
            process.wait(timeout=2)
        except Exception:
            pass
        raise

    return ChBootResult(
        vm_id=vm_id,
        vm_dir=vm_dir,
        socket_path=socket_path,
        ns_name=ns_name,
        process=process,
        agent_url=agent_url,
        tap_name=tap_name,
        veth_ip="",
        timings=timings,
        t_total=t_total,
    )


def ch_create_golden(dest_dir: str = CH_GOLDEN_DIR, source_rootfs: str = SOURCE_ROOTFS) -> None:
    """Install the default CH template (just a rootfs copy, no snapshot)."""
    try:
        source_size = os.path.getsize(source_rootfs)
    except FileNotFoundError as exc:
        raise RuntimeError(f"source rootfs not found: {source_rootfs}") from exc
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, "rootfs.ext4")
    try:
        if os.path.getsize(dest) == source_size:
            return
    except FileNotFoundError:
        pass
    subprocess.run(["cp", "--reflink=auto", source_rootfs, dest], check=True)
    log.info("CH golden rootfs installed: %s (%d bytes)", dest, source_size)


def ch_golden_ready(dest_dir: str = CH_GOLDEN_DIR) -> bool:
    return os.path.exists(os.path.join(dest_dir, "rootfs.ext4"))

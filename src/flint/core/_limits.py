"""Host-enforced resource ceilings shared by both Linux VMMs."""
from __future__ import annotations

import os
import json
from pathlib import Path
import subprocess
import signal
import time

from . import config


def _positive_env(name: str, default: int) -> int:
    value = int(os.environ.get(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


DISK_BPS = _positive_env("FLINT_DISK_BYTES_PER_SECOND", 100 * 1024 * 1024)
NETWORK_BPS = _positive_env("FLINT_NETWORK_BYTES_PER_SECOND", 12_500_000)


def _whole_device(device: int, sysfs: Path = Path("/sys/dev/block")) -> str:
    """io.max rejects partitions: resolve their parent disk through sysfs."""
    if not os.major(device):
        raise RuntimeError("Disk IO limits require a local block-backed filesystem")
    node = (sysfs / f"{os.major(device)}:{os.minor(device)}").resolve(strict=True)
    if (node / "partition").exists():
        node = node.parent
    return (node / "dev").read_text().strip()


def vm_cgroup(vm_id: str, kind: str) -> str:
    parent = config.CH_PARENT_CGROUP if kind == "linux-cloud-hypervisor" else config.JAILER_PARENT_CGROUP
    return f"/sys/fs/cgroup/{parent}/{vm_id}"


def attach_process(path: str, pid: int) -> None:
    group = Path(path)
    group.mkdir(parents=True, exist_ok=True)
    (group / "cgroup.procs").write_text(str(pid))
    verify_process(path, pid)


def verify_process(path: str, pid: int) -> None:
    group = Path(path)
    expected = "0::/" + str(group.relative_to("/sys/fs/cgroup"))
    if expected not in Path(f"/proc/{pid}/cgroup").read_text().splitlines():
        raise RuntimeError(f"Process {pid} did not enter {path}")


def owns_process(pid: int, root: str) -> bool:
    try:
        if Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z":
            return False
        return bool(pid and root and os.path.samestat(os.stat(f"/proc/{pid}/root"), os.stat(root)))
    except FileNotFoundError:
        return False


def stop_processes(path: str, *, pid: int = 0, root: str = "", process=None) -> None:
    """Stop only this jail's processes; prove the cgroup empty before cleanup."""
    group = Path(path)
    if group.exists():
        (group / "cgroup.kill").write_text("1")
    # Older versions may have left the VMM outside its intended cgroup.
    if owns_process(pid, root):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if isinstance(process, subprocess.Popen):
        process.wait(timeout=10)
    deadline = time.monotonic() + 10
    while True:
        populated = group.exists() and "populated 1" in (group / "cgroup.events").read_text()
        alive = owns_process(pid, root)
        if not populated and not alive:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(f"VMM has not stopped; retaining jail {root}")
        time.sleep(.01)


def ch_disk_limiter() -> dict:
    return {"bandwidth": {"size": DISK_BPS, "refill_time": 1000}}


def cgroup_limits(path: str, rootfs: str, *, vcpus: int = 1, memory_mib: int = 128, vmm_disk_limit: bool = False) -> None:
    """Apply limits before the guest runs; absence of cgroup v2 is an error."""
    root = Path("/sys/fs/cgroup")
    if config.JAILER_CGROUP_VER != 2:
        raise RuntimeError("Phase 1 isolation requires cgroup v2")
    group = Path(path)
    parents = list(reversed(group.parents))
    for parent in parents:
        if parent != root and root not in parent.parents:
            continue
        parent.mkdir(exist_ok=True)
        with (parent / "cgroup.subtree_control").open("w") as f:
            f.write("+cpu +memory +io +pids")
    group.mkdir(exist_ok=True)
    (group / "cpu.max").write_text(f"{vcpus * 100000} 100000")
    (group / "memory.max").write_text(str((memory_mib + 256) * 1024 * 1024))
    (group / "pids.max").write_text("512")
    device = os.stat(rootfs).st_dev
    if os.major(device) or not vmm_disk_limit:
        device = _whole_device(device)
        (group / "io.max").write_text(f"{device} rbps={DISK_BPS} wbps={DISK_BPS}")


def network_limits(ns_name: str, tap_name: str) -> None:
    """Shape traffic to the guest and police traffic from it, including storage."""
    prefix = ["ip", "netns", "exec", ns_name, "tc"]
    rate = str(NETWORK_BPS * 8) + "bit"
    subprocess.run(prefix + ["qdisc", "replace", "dev", tap_name, "root", "tbf", "rate", rate,
                             "burst", "131072", "latency", "50ms"], check=True, capture_output=True)
    subprocess.run(prefix + ["qdisc", "replace", "dev", tap_name, "handle", "ffff:", "ingress"],
                   check=True, capture_output=True)
    # matchall rejects replacement even with a stable handle. Use a u32
    # catch-all with an explicit handle so recovery can update it atomically.
    subprocess.run(prefix + ["filter", "replace", "dev", tap_name, "parent", "ffff:", "protocol", "all",
                             "pref", "2", "handle", "800::1", "u32", "match", "u32", "0", "0",
                             "action", "police", "rate", rate,
                             "burst", "131072", "conform-exceed", "drop"], check=True, capture_output=True)
    # Upgrade the original rule only after its replacement is enforcing the
    # ceiling. Removing it first would briefly leave a live guest unpoliced.
    legacy = subprocess.run(prefix + ["-j", "filter", "show", "dev", tap_name, "parent", "ffff:", "pref", "1"],
                            check=True, capture_output=True, text=True)
    if any(rule.get("kind") == "matchall" for rule in json.loads(legacy.stdout)):
        subprocess.run(prefix + ["filter", "del", "dev", tap_name, "parent", "ffff:", "protocol", "all",
                                 "pref", "1", "matchall"], check=True, capture_output=True)

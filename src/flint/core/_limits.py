"""Host-enforced resource ceilings shared by both Linux VMMs."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess

from . import config


def _positive_env(name: str, default: int) -> int:
    value = int(os.environ.get(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


DISK_BPS = _positive_env("FLINT_DISK_BYTES_PER_SECOND", 100 * 1024 * 1024)
NETWORK_BPS = _positive_env("FLINT_NETWORK_BYTES_PER_SECOND", 12_500_000)


def cgroup_limits(path: str, rootfs: str, *, vcpus: int = 1, memory_mib: int = 128) -> None:
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
    if not os.major(device):
        raise RuntimeError(f"Cannot enforce disk IO limits on the filesystem holding {rootfs}")
    (group / "io.max").write_text(f"{os.major(device)}:{os.minor(device)} rbps={DISK_BPS} wbps={DISK_BPS}")


def network_limits(ns_name: str, tap_name: str) -> None:
    """Shape traffic to the guest and police traffic from it, including storage."""
    prefix = ["ip", "netns", "exec", ns_name, "tc"]
    rate = str(NETWORK_BPS * 8) + "bit"
    subprocess.run(prefix + ["qdisc", "replace", "dev", tap_name, "root", "tbf", "rate", rate,
                             "burst", "131072", "latency", "50ms"], check=True, capture_output=True)
    subprocess.run(prefix + ["qdisc", "replace", "dev", tap_name, "handle", "ffff:", "ingress"],
                   check=True, capture_output=True)
    subprocess.run(prefix + ["filter", "replace", "dev", tap_name, "parent", "ffff:", "protocol", "all",
                             "pref", "1", "matchall", "action", "police", "rate", rate,
                             "burst", "131072", "conform-exceed", "drop"], check=True, capture_output=True)

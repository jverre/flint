"""Firecracker jailer integration - chroot, cgroup, seccomp, and UID/GID isolation."""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass

from .config import (
    JAILER_BINARY, FIRECRACKER_BINARY, JAILER_BASE_DIR,
    JAILER_UID, JAILER_GID, JAILER_CGROUP_VER, log,
)


@dataclass
class JailSpec:
    vm_id: str
    ns_name: str

    @property
    def chroot_base(self) -> str:
        return f"{JAILER_BASE_DIR}/{os.path.basename(FIRECRACKER_BINARY)}/{self.vm_id}"

    @property
    def chroot_root(self) -> str:
        return f"{self.chroot_base}/root"

    @property
    def socket_path_on_host(self) -> str:
        return f"{self.chroot_root}/firecracker.sock"


def stage_file_into_chroot(src: str, dest_name: str, spec: JailSpec) -> str:
    """Hard-link (fallback: copy) src into the chroot root. Returns dest path."""
    dest = f"{spec.chroot_root}/{dest_name}"
    try:
        os.link(src, dest)
    except OSError:
        log.warning("jailer: hard-link failed for %s, falling back to copy", os.path.basename(src))
        shutil.copy2(src, dest)
    os.chown(dest, JAILER_UID, JAILER_GID)
    return dest


def build_jailer_command(spec: JailSpec) -> list[str]:
    return [
        JAILER_BINARY,
        "--id", spec.vm_id,
        "--exec-file", FIRECRACKER_BINARY,
        "--uid", str(JAILER_UID),
        "--gid", str(JAILER_GID),
        "--chroot-base-dir", JAILER_BASE_DIR,
        "--cgroup-version", str(JAILER_CGROUP_VER),
        "--netns", f"/var/run/netns/{spec.ns_name}",
        "--",
        "--api-sock", "firecracker.sock",
    ]


def cleanup_jailer(chroot_base: str, vm_id: str) -> None:
    """Remove chroot tree and cgroup entries."""
    if os.path.isdir(chroot_base):
        shutil.rmtree(chroot_base, ignore_errors=True)
    if JAILER_CGROUP_VER == 2:
        cgroup_path = f"/sys/fs/cgroup/firecracker/{vm_id}"
        if os.path.isdir(cgroup_path):
            shutil.rmtree(cgroup_path, ignore_errors=True)
    else:
        for controller in ("cpu", "memory", "blkio", "devices", "pids"):
            cgroup_path = f"/sys/fs/cgroup/{controller}/firecracker/{vm_id}"
            if os.path.isdir(cgroup_path):
                shutil.rmtree(cgroup_path, ignore_errors=True)

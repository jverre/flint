"""Firecracker jailer integration - chroot, cgroup, seccomp, and UID/GID isolation."""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass

from .config import (
    JAILER_BINARY, FIRECRACKER_BINARY, JAILER_BASE_DIR,
    JAILER_UID, JAILER_GID, JAILER_CGROUP_VER, JAILER_SHARED_DIR, JAILER_PARENT_CGROUP,
)
from ._artifacts import link_into, shared_readonly
from ._jailfs import own


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


def stage_private_file(src: str, dest_name: str, spec: JailSpec) -> str:
    """Move a VM-owned writable file (e.g. a pool rootfs) into the chroot. Returns dest path."""
    dest = f"{spec.chroot_root}/{dest_name}"
    link_into(src, dest)
    make_private(dest)
    return dest


def make_private(path: str) -> None:
    """Hand a per-VM file to the jailed VMM. Copies of read-only golden files inherit 0444."""
    own(path, JAILER_UID, JAILER_GID, 0o600)


def stage_shared_file(src: str, dest_name: str, spec: JailSpec) -> str:
    """Hard-link a read-only artifact shared by all VMs (golden mem/vmstate) into the chroot.

    The link shares the inode with every other VM, so it stays root-owned and
    read-only: a compromised VMM must not be able to rewrite another tenant's
    snapshot. Sharing the inode also shares the page cache across VMs.
    """
    dest = f"{spec.chroot_root}/{dest_name}"
    os.link(shared_readonly(src, JAILER_SHARED_DIR), dest)
    return dest


def build_jailer_command(spec: JailSpec, *, vcpus: int = 1, memory_mib: int = 128) -> list[str]:
    from ._limits import cgroup_limits
    cgroup_limits(f"/sys/fs/cgroup/{JAILER_PARENT_CGROUP}/{spec.vm_id}",
                  f"{spec.chroot_root}/rootfs.ext4", vcpus=vcpus, memory_mib=memory_mib)
    return [
        JAILER_BINARY,
        "--id", spec.vm_id,
        "--exec-file", FIRECRACKER_BINARY,
        "--uid", str(JAILER_UID),
        "--gid", str(JAILER_GID),
        "--chroot-base-dir", JAILER_BASE_DIR,
        "--cgroup-version", str(JAILER_CGROUP_VER),
        "--parent-cgroup", JAILER_PARENT_CGROUP,
        "--netns", f"/var/run/netns/{spec.ns_name}",
        "--",
        "--api-sock", "firecracker.sock",
    ]


def cleanup_jailer(chroot_base: str, vm_id: str) -> None:
    """Wipe and remove the chroot tree, and remove the cgroup entries."""
    from ._wipe import wipe_tree
    if os.path.isdir(chroot_base):
        wipe_tree(chroot_base, vm_id)
    if JAILER_CGROUP_VER == 2:
        cgroup_path = f"/sys/fs/cgroup/{JAILER_PARENT_CGROUP}/{vm_id}"
        if os.path.isdir(cgroup_path):
            os.rmdir(cgroup_path)
    else:
        for controller in ("cpu", "memory", "blkio", "devices", "pids"):
            cgroup_path = f"/sys/fs/cgroup/{controller}/{JAILER_PARENT_CGROUP}/{vm_id}"
            if os.path.isdir(cgroup_path):
                shutil.rmtree(cgroup_path, ignore_errors=True)

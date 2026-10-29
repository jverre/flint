"""Jail for cloud-hypervisor, matching what the Firecracker jailer gives FC.

cloud-hypervisor ships no jailer, so Flint builds one from standard tools:

- ``ip netns exec`` runs it in the VM's network namespace (and a private mount
  namespace);
- ``setpriv --no-new-privs`` stops it regaining privileges through exec;
- ``chroot --userspec`` confines it to ``<JAILER_BASE_DIR>/cloud-hypervisor/<id>/root``
  as its own numeric workload identity with no supplementary groups or capabilities.

The chroot holds only what the VMM needs: its binary, the kernel and golden
snapshot files (shared, root-owned, read-only), the VM's private disk, and
``/dev/kvm`` + ``/dev/urandom``. There is no ``/dev/net/tun`` and no ``/proc``:
the daemon opens the TAP and passes the fd, so the VMM never needs
CAP_NET_ADMIN, which would let it rewrite its namespace's firewall. Each VMM
also gets its own cgroup and runs with ``--seccomp true``. The log lives outside
the chroot so the VMM can't rewrite it.

On a migration network (``FLINT_SHARED_ROOTFS_DIR``) the VMM opens its TAP
itself, so the chroot then has ``/dev/net/tun``.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from typing import ClassVar

from ... import config
from ...config import log
from ..._artifacts import shared_readonly
from ..common import stop_process
from ..jail import Jail
from . import api

# Paths as the VMM sees them inside its chroot.
JAIL_BINARY = "/cloud-hypervisor"
JAIL_KERNEL = "/vmlinux"
JAIL_ROOTFS = "/rootfs.ext4"


def jail_dir() -> str:
    """Where every CH jail lives."""
    return f"{config.JAILER_BASE_DIR}/cloud-hypervisor"


@dataclass(frozen=True)
class CloudHypervisorJail(Jail):
    SOCKET: ClassVar[str] = "/ch.sock"
    LOG_NAME: ClassVar[str] = "cloud-hypervisor.log"

    @property
    def base(self) -> str:
        return f"{jail_dir()}/{self.vm_id}"

    @property
    def cgroup(self) -> str:
        return f"/sys/fs/cgroup/{config.CH_PARENT_CGROUP}/{self.vm_id}"

    @property
    def shared_disk_marker(self) -> str:
        """Present while the root disk is a bind mount of a shared disk; outside the chroot."""
        return f"{self.base}/shared-rootfs"

    def prepare(self) -> None:
        """Create the chroot with the VMM binary, kernel and device nodes."""
        os.makedirs(f"{self.root}/dev", exist_ok=True)
        os.chown(self.root, *self.identity)
        kvm = self.host_path("/dev/kvm")
        os.mknod(kvm, 0o600 | stat.S_IFCHR, os.makedev(10, 232))
        os.chown(kvm, *self.identity)
        os.mknod(self.host_path("/dev/urandom"), 0o444 | stat.S_IFCHR, os.makedev(1, 9))
        if config.SHARED_ROOTFS_DIR:
            os.mkdir(self.host_path("/dev/net"))
            os.mknod(self.host_path("/dev/net/tun"), 0o600 | stat.S_IFCHR, os.makedev(10, 200))
            os.chown(self.host_path("/dev/net/tun"), *self.identity)
        os.link(shared_readonly(_ch_binary(), config.JAILER_SHARED_DIR, mode=0o555), self.host_path(JAIL_BINARY))
        os.link(shared_readonly(config.KERNEL_PATH, config.JAILER_SHARED_DIR), self.host_path(JAIL_KERNEL))

    def spawn(self, ns_name: str, *, vcpus: int | None = None, memory_mib: int | None = None) -> subprocess.Popen:
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        uid, _ = self.identity
        cmd = [
            "ip", "netns", "exec", ns_name,
            "setpriv", "--no-new-privs", "--",
            "chroot", f"--userspec={uid}:{uid}", "--groups=", self.root,
            JAIL_BINARY, "--api-socket", f"path={self.SOCKET}", "--seccomp", "true",
        ]
        with open(self.log_path, "a") as log_fd:
            process = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=log_fd, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        try:
            self._enter_cgroup(process.pid, vcpus=vcpus, memory_mib=memory_mib)
            api.wait_for_api_socket(self.socket_path, process=process, log_path=self.log_path)
        except BaseException:
            stop_process(process)
            raise
        return process

    def _enter_cgroup(self, pid: int, *, vcpus: int | None = None, memory_mib: int | None = None) -> None:
        from ..._limits import attach_process, cgroup_limits
        cgroup_limits(self.cgroup, self.host_path(JAIL_ROOTFS), vcpus=vcpus or config.CH_CPU_COUNT,
                      memory_mib=memory_mib or config.CH_MEMORY_BYTES // (1 << 20), vmm_disk_limit=True)
        # ip/setpriv/chroot exec into the VMM, so the pid is the VMM's for its lifetime.
        if config.JAILER_CGROUP_VER != 2:
            log.warning("cloud-hypervisor jail: cgroup v1 not supported, VMM %s runs without its own cgroup", self.vm_id[:8])
            return
        attach_process(self.cgroup, pid)

    def cleanup(self) -> None:
        from ..._identities import release
        from ..._limits import stop_processes
        from ..._wipe import wipe_tree
        stop_processes(self.cgroup)
        if os.path.exists(self.shared_disk_marker):
            target = self.host_path(JAIL_ROOTFS)
            # Never lazily unmount a disk a VMM might still be using.
            if subprocess.run(["mountpoint", "-q", target]).returncode == 0:
                subprocess.run(["umount", target], check=True, capture_output=True, timeout=30)
            os.unlink(self.shared_disk_marker)
        # Wipe only once the shared disk is unmounted: it isn't this jail's to wipe.
        wipe_tree(self.base, self.vm_id)
        try:
            os.rmdir(self.cgroup)
        except OSError:
            pass
        release("vm:" + self.vm_id)


def _ch_binary() -> str:
    binary = config.CH_BINARY
    if os.path.exists(binary):
        return binary
    return shutil.which("cloud-hypervisor") or binary

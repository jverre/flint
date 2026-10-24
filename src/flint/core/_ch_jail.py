"""Jail for cloud-hypervisor, matching what the Firecracker jailer gives FC.

cloud-hypervisor ships no jailer, so Flint builds one from standard tools:

- ``ip netns exec`` runs it in the VM's network namespace (and a private mount
  namespace);
- ``setpriv --no-new-privs`` stops it regaining privileges through exec;
- ``chroot --userspec`` confines it to ``<JAILER_BASE_DIR>/cloud-hypervisor/<id>/root``
  as ``CH_UID`` with no supplementary groups and no capabilities.

The chroot holds only what the VMM needs: its binary, the kernel and golden
snapshot files (shared, root-owned, read-only), the VM's private disk, and
``/dev/kvm`` + ``/dev/urandom``. There is no ``/dev/net/tun`` and no ``/proc``:
the daemon opens the TAP and passes the fd, so the VMM never needs
CAP_NET_ADMIN, which would let it rewrite its namespace's firewall. Each VMM
also gets its own cgroup and runs with ``--seccomp true``. The log lives outside
the chroot so the VMM can't rewrite it.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from dataclasses import dataclass

from . import config
from . import _cloud_hypervisor as _ch
from ._artifacts import shared_readonly
from ._jailfs import own
from .config import JAILER_BASE_DIR, JAILER_SHARED_DIR, log

CH_JAIL_DIR = f"{JAILER_BASE_DIR}/cloud-hypervisor"
CH_CGROUP_ROOT = f"/sys/fs/cgroup/{os.environ.get('FLINT_CH_PARENT_CGROUP', 'cloud-hypervisor')}"

# Paths as the VMM sees them inside its chroot.
JAIL_BINARY = "/cloud-hypervisor"
JAIL_KERNEL = "/vmlinux"
JAIL_ROOTFS = "/rootfs.ext4"
JAIL_SOCKET = "/ch.sock"


@dataclass(frozen=True)
class ChJail:
    vm_id: str

    @property
    def base(self) -> str:
        return f"{CH_JAIL_DIR}/{self.vm_id}"

    @property
    def root(self) -> str:
        return f"{self.base}/root"

    @property
    def socket_path(self) -> str:
        return self.host_path(JAIL_SOCKET)

    @property
    def log_path(self) -> str:
        return f"{self.base}/cloud-hypervisor.log"

    @property
    def cgroup(self) -> str:
        return f"{CH_CGROUP_ROOT}/{self.vm_id}"

    def host_path(self, jail_path: str) -> str:
        return self.root + jail_path

    def prepare(self) -> None:
        """Create the chroot with the VMM binary, kernel and device nodes."""
        os.makedirs(f"{self.root}/dev", exist_ok=True)
        os.chown(self.root, config.CH_UID, config.CH_UID)
        kvm = self.host_path("/dev/kvm")
        os.mknod(kvm, 0o600 | stat.S_IFCHR, os.makedev(10, 232))
        os.chown(kvm, config.CH_UID, config.CH_UID)
        os.mknod(self.host_path("/dev/urandom"), 0o444 | stat.S_IFCHR, os.makedev(1, 9))
        if os.environ.get("FLINT_SHARED_ROOTFS_DIR"):
            os.mkdir(self.host_path("/dev/net"))
            os.mknod(self.host_path("/dev/net/tun"), 0o600 | stat.S_IFCHR, os.makedev(10, 200))
            os.chown(self.host_path("/dev/net/tun"), config.CH_UID, config.CH_UID)
        os.link(shared_readonly(_ch_binary(), JAILER_SHARED_DIR, mode=0o555), self.host_path(JAIL_BINARY))
        os.link(shared_readonly(config.KERNEL_PATH, JAILER_SHARED_DIR), self.host_path(JAIL_KERNEL))

    def give(self, jail_path: str) -> str:
        """Make a file or directory in the chroot owned and writable by the VMM. Returns its host path."""
        path = self.host_path(jail_path)
        own(path, config.CH_UID, config.CH_UID, 0o700 if os.path.isdir(path) else 0o600)
        return path

    def link_shared(self, src: str, jail_path: str) -> None:
        """Hard-link a read-only artifact shared by all VMs into the chroot."""
        os.link(shared_readonly(src, JAILER_SHARED_DIR), self.host_path(jail_path))

    def spawn(self, ns_name: str, *, vcpus: int | None = None, memory_mib: int | None = None) -> subprocess.Popen:
        """Start the jailed VMM and wait for its API socket."""
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        uid = config.CH_UID
        cmd = [
            "ip", "netns", "exec", ns_name,
            "setpriv", "--no-new-privs", "--",
            "chroot", f"--userspec={uid}:{uid}", f"--groups={uid}", self.root,
            JAIL_BINARY, "--api-socket", f"path={JAIL_SOCKET}", "--seccomp", "true",
        ]
        with open(self.log_path, "a") as log_fd:
            process = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=log_fd, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        try:
            self._enter_cgroup(process.pid, vcpus=vcpus, memory_mib=memory_mib)
            _ch.wait_for_api_socket(self.socket_path, process=process, log_path=self.log_path)
        except Exception:
            process.kill()
            process.wait(timeout=2)
            raise
        return process

    def _enter_cgroup(self, pid: int, *, vcpus: int | None = None, memory_mib: int | None = None) -> None:
        from ._limits import cgroup_limits
        cgroup_limits(self.cgroup, self.host_path(JAIL_ROOTFS), vcpus=vcpus or config.CH_CPU_COUNT,
                      memory_mib=memory_mib or config.CH_MEMORY_BYTES // (1 << 20))
        # ip/setpriv/chroot exec into the VMM, so the pid is the VMM's for its lifetime.
        if config.JAILER_CGROUP_VER != 2:
            log.warning("cloud-hypervisor jail: cgroup v1 not supported, VMM %s runs without its own cgroup", self.vm_id[:8])
            return
        os.makedirs(self.cgroup, exist_ok=True)
        with open(f"{self.cgroup}/cgroup.procs", "w") as f:
            f.write(str(pid))

    def cleanup(self) -> None:
        marker = f"{self.base}/shared-rootfs"
        if os.path.exists(marker):
            target = self.host_path(JAIL_ROOTFS)
            # Never lazily unmount a disk a VMM might still be using.
            if subprocess.run(["mountpoint", "-q", target]).returncode == 0:
                subprocess.run(["umount", target], check=True, capture_output=True, timeout=30)
            os.unlink(marker)
        # Wipe only once the shared disk is unmounted: it isn't this jail's to wipe.
        from ._wipe import wipe_tree
        wipe_tree(self.base, self.vm_id)
        try:
            os.rmdir(self.cgroup)
        except OSError:
            pass


def _ch_binary() -> str:
    binary = config.CH_BINARY
    if os.path.exists(binary):
        return binary
    return shutil.which("cloud-hypervisor") or binary

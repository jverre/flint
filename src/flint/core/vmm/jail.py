"""What every VMM runs in: a per-VM chroot, identity and cgroup.

A :class:`Jail` is named by its VM's ID; everything else (directories, the
API socket, the cgroup, the host identity) derives from it, so a jail can be
found again after a daemon restart from nothing but the ID. Subclasses say
how their VMM is started inside it (Firecracker's jailer, or ``chroot`` +
``setpriv`` for cloud-hypervisor) and where its files live.

The VMM runs guest code, so it is untrusted: files it can write are owned by
its identity, files shared between VMs are hard links to root-owned
read-only copies, and logs live outside the chroot where it can't rewrite them.
"""

from __future__ import annotations

import os
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from ..config import JAILER_SHARED_DIR
from .._artifacts import link_into, shared_readonly
from .._identities import vm_identity
from .._jailfs import own


@dataclass(frozen=True)
class Jail(ABC):
    vm_id: str

    # The API socket's path inside the chroot.
    SOCKET: ClassVar[str]
    # The VMM's log, in the jail's base directory (outside the chroot).
    LOG_NAME: ClassVar[str]

    @property
    @abstractmethod
    def base(self) -> str:
        """The jail's directory: the chroot plus host-only files (log, markers)."""

    @property
    @abstractmethod
    def cgroup(self) -> str:
        """The VMM's own cgroup (v2 path)."""

    @property
    def identity(self) -> tuple[int, int]:
        """(uid, gid) the VMM runs as: unique to this VM."""
        return vm_identity(self.vm_id)

    @property
    def root(self) -> str:
        """The chroot."""
        return f"{self.base}/root"

    @property
    def socket_path(self) -> str:
        return self.host_path(self.SOCKET)

    @property
    def log_path(self) -> str:
        return f"{self.base}/{self.LOG_NAME}"

    def host_path(self, jail_path: str) -> str:
        """The host path of ``jail_path`` (absolute inside the chroot)."""
        return self.root + jail_path

    def give(self, jail_path: str) -> str:
        """Make a file or directory in the chroot owned and writable by the VMM only. Returns its host path."""
        path = self.host_path(jail_path)
        own(path, *self.identity, 0o700 if os.path.isdir(path) else 0o600)
        return path

    def stage_private(self, src: str, jail_path: str) -> str:
        """Move a per-VM writable file (e.g. a pooled disk copy) into the chroot and give it to the VMM."""
        link_into(src, self.host_path(jail_path))
        return self.give(jail_path)

    def link_shared(self, src: str, jail_path: str) -> str:
        """Hard-link a read-only artifact shared by all VMs (golden memory, kernel) into the chroot.

        The link shares the inode with every other VM, so it stays root-owned
        and read-only: a compromised VMM must not be able to rewrite another
        tenant's snapshot. Sharing the inode also shares the page cache.
        """
        dest = self.host_path(jail_path)
        os.link(shared_readonly(src, JAILER_SHARED_DIR), dest)
        return dest

    @abstractmethod
    def prepare(self) -> None:
        """Create the chroot, owned by the VMM's identity."""

    @abstractmethod
    def spawn(self, ns_name: str, *, vcpus: int | None = None, memory_mib: int | None = None) -> subprocess.Popen:
        """Start the jailed VMM in network namespace ``ns_name`` and wait for its API socket."""

    @abstractmethod
    def cleanup(self) -> None:
        """Stop anything left in the jail's cgroup, wipe and remove the jail, release the identity."""

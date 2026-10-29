"""Firecracker's jailer: chroot, cgroup, seccomp and a per-VM UID/GID.

The jailer creates ``<JAILER_BASE_DIR>/firecracker/<id>/root``, joins the
VM's cgroup and network namespace, drops to the VM's identity and execs
Firecracker with its API socket at ``/firecracker.sock`` in the chroot.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import ClassVar

from ... import config
from ..common import stop_process
from ..jail import Jail
from . import api


@dataclass(frozen=True)
class FirecrackerJail(Jail):
    SOCKET: ClassVar[str] = "/firecracker.sock"
    LOG_NAME: ClassVar[str] = "firecracker.log"

    @property
    def base(self) -> str:
        return f"{config.JAILER_BASE_DIR}/{os.path.basename(config.FIRECRACKER_BINARY)}/{self.vm_id}"

    @property
    def cgroup(self) -> str:
        return f"/sys/fs/cgroup/{config.JAILER_PARENT_CGROUP}/{self.vm_id}"

    def prepare(self) -> None:
        os.makedirs(self.root, exist_ok=True)
        os.chown(self.root, *self.identity)

    def command(self, ns_name: str, *, vcpus: int = 1, memory_mib: int = 128) -> list[str]:
        """The jailer command line. Creates the VM's cgroup with its limits first."""
        from ..._limits import cgroup_limits
        cgroup_limits(self.cgroup, self.host_path("/rootfs.ext4"), vcpus=vcpus, memory_mib=memory_mib)
        uid, gid = self.identity
        return [
            config.JAILER_BINARY,
            "--id", self.vm_id,
            "--exec-file", config.FIRECRACKER_BINARY,
            "--uid", str(uid),
            "--gid", str(gid),
            "--chroot-base-dir", config.JAILER_BASE_DIR,
            "--cgroup-version", str(config.JAILER_CGROUP_VER),
            # With no --cgroup properties the jailer joins this existing group
            # directly. Its parent has controllers delegated and cannot hold tasks.
            "--parent-cgroup", f"{config.JAILER_PARENT_CGROUP}/{self.vm_id}",
            "--netns", f"/var/run/netns/{ns_name}",
            "--",
            "--api-sock", self.SOCKET.lstrip("/"),
        ]

    def spawn(self, ns_name: str, *, vcpus: int | None = None, memory_mib: int | None = None) -> subprocess.Popen:
        cmd = self.command(ns_name, vcpus=vcpus or 1, memory_mib=memory_mib or 128)
        with open(self.log_path, "w") as log_fd:
            process = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=log_fd, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        try:
            api.wait_for_api_socket(self.socket_path, process=process, log_path=self.log_path, cgroup=self.cgroup)
        except BaseException:
            stop_process(process)
            raise
        return process

    def reset(self, keep: tuple[str, ...]) -> None:
        """Clear what a previous jailer run left (device nodes, binary link,
        cgroup), keeping the VM's own files: a jailer restarted with the
        same ``--id`` refuses to overwrite them."""
        for name in os.listdir(self.root):
            if name in keep:
                continue
            path = os.path.join(self.root, name)
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                try:
                    os.unlink(path)
                except OSError:
                    pass
        for controller in ("", "cpu/", "memory/", "pids/"):
            group = f"/sys/fs/cgroup/{controller}{config.JAILER_PARENT_CGROUP}/{self.vm_id}"
            if os.path.isdir(group):
                shutil.rmtree(group, ignore_errors=True)

    def cleanup(self) -> None:
        from ..._identities import release
        from ..._limits import stop_processes
        from ..._wipe import wipe_tree
        stop_processes(self.cgroup)
        if os.path.isdir(self.base):
            wipe_tree(self.base, self.vm_id)
        if config.JAILER_CGROUP_VER == 2:
            if os.path.isdir(self.cgroup):
                os.rmdir(self.cgroup)
        else:
            for controller in ("cpu", "memory", "blkio", "devices", "pids"):
                group = f"/sys/fs/cgroup/{controller}/{config.JAILER_PARENT_CGROUP}/{self.vm_id}"
                if os.path.isdir(group):
                    shutil.rmtree(group, ignore_errors=True)
        release("vm:" + self.vm_id)

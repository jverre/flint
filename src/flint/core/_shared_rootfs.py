"""Shared POSIX root disks for migratable Cloud Hypervisor VMs.

The pool must be an already mounted NFS/CEPH/cluster filesystem. VM disks are
bind mounted into the jail at /rootfs.ext4; deleting a jail never deletes data.
The control plane and snapd fences remain responsible for single VM ownership.
"""

from __future__ import annotations

import fcntl
import contextlib
import contextvars
import re
import json
import os
from pathlib import Path
import stat
import subprocess
import uuid

from ._artifacts import sparse_copy

_SHARED_FS = {"nfs", "nfs4", "ceph", "gfs2", "ocfs2"}


_replace_authorized = contextvars.ContextVar("shared_disk_replacement", default=False)


@contextlib.contextmanager
def disk_replacement(authorized: bool):
    token = _replace_authorized.set(authorized)
    try:
        yield
    finally:
        _replace_authorized.reset(token)


def _run(*args: str) -> str:
    return subprocess.run(
        list(args), check=True, capture_output=True, text=True, timeout=30
    ).stdout


def _uuid(value: str) -> str:
    parsed = str(uuid.UUID(value))
    if value != parsed:
        raise ValueError("VM and pool IDs must be canonical UUIDs")
    return parsed


class SharedRootfs:
    def __init__(self, directory: str) -> None:
        self.directory = Path(directory)
        if not self.directory.is_absolute() or self.directory.is_symlink():
            raise ValueError("shared rootfs pool must be an absolute, non-symlink path")
        st = self.directory.stat()
        if st.st_uid != 0 or st.st_mode & 0o022:
            raise ValueError(
                "shared rootfs pool must be root owned and not group/world writable"
            )
        mounts = json.loads(
            _run(
                "findmnt",
                "--json",
                "--target",
                str(self.directory),
                "--output",
                "FSTYPE,OPTIONS",
            )
        )
        mount = mounts["filesystems"][0]
        if mount["fstype"] not in _SHARED_FS or "rw" not in mount["options"].split(","):
            raise ValueError(
                "root disk pool requires a writable shared POSIX filesystem"
            )
        marker = self.directory / "pool.json"
        self.pool_id = _uuid(self._read_json(marker)["id"])

    @classmethod
    def initialize(cls, directory: str) -> "SharedRootfs":
        path = Path(directory)
        if not path.is_absolute() or path.is_symlink():
            raise ValueError("pool must be an absolute, non-symlink directory")
        st = path.stat()
        if st.st_uid != 0 or st.st_mode & 0o022:
            raise ValueError("pool must be root owned and not group/world writable")
        mount = json.loads(
            _run(
                "findmnt", "--json", "--target", str(path), "--output", "FSTYPE,OPTIONS"
            )
        )["filesystems"][0]
        if mount["fstype"] not in _SHARED_FS or "rw" not in mount["options"].split(","):
            raise ValueError(
                "root disk pool requires a writable shared POSIX filesystem"
            )
        fd = os.open(
            path / "pool.json",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o644,
        )
        with os.fdopen(fd, "w") as stream:
            json.dump({"id": str(uuid.uuid4())}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        return cls(directory)

    @staticmethod
    def _read_json(path: Path) -> dict:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as stream:
            st = os.fstat(stream.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022:
                raise ValueError("untrusted shared rootfs metadata")
            return json.load(stream)

    def descriptor(self, vm_id: str) -> dict:
        vm_id = _uuid(vm_id)
        path = self.directory / vm_id
        if path.is_symlink():
            raise ValueError("VM root disk directory is a symlink")
        st = path.stat()
        if st.st_uid != 0 or st.st_mode & 0o022:
            raise ValueError("untrusted VM root disk directory")
        result = self._read_json(path / "disk.json")
        if result.get("vm_id") != vm_id or result.get("pool_id") != self.pool_id:
            raise ValueError("root disk identity mismatch")
        if not 1 <= result.get("vni", 0) <= 0xFFFFFF:
            raise ValueError("invalid migration network ID")
        return result

    def create(self, vm_id: str, source: str, uid: int) -> dict:
        """Create once under a pool lock; never overwrite a VM's existing disk."""
        vm_id = _uuid(vm_id)
        # This lock is supported by the configured shared filesystem, including
        # NFSv4. It serialises allocation across all hosts, not just this process.
        fd = os.open(
            self.directory / ".allocation.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            path = self.directory / vm_id
            if path.exists():
                if not _replace_authorized.get():
                    raise FileExistsError(
                        "shared disk already exists; replacement requires control-plane recovery authority"
                    )
                current = self.descriptor(vm_id)
                return self._write_generation(
                    path,
                    source,
                    uid,
                    current["vni"],
                    f"rootfs-{uuid.uuid4()}.ext4",
                    vm_id,
                )
            path.mkdir(mode=0o700)
            try:
                used = {
                    self.descriptor(p.name)["vni"]
                    for p in self.directory.iterdir()
                    if p.is_dir()
                    and p.name != vm_id
                    and not p.name.startswith(".")
                    and (p / "disk.json").exists()
                }
                vni = (uuid.UUID(vm_id).int % 0xFFFFFF) + 1
                while vni in used:
                    vni = vni % 0xFFFFFF + 1
                    if len(used) >= 0xFFFFFF:
                        raise RuntimeError("migration network IDs exhausted")
                return self._write_generation(
                    path, source, uid, vni, "rootfs.ext4", vm_id
                )
            except Exception:
                # Preserve failed allocations for inspection, never silently reuse.
                raise
        finally:
            os.close(fd)

    def _write_generation(
        self, path: Path, source: str, uid: int, vni: int, filename: str, vm_id: str
    ) -> dict:
        disk = path / filename
        sparse_copy(source, str(disk))
        os.chown(disk, uid, uid)
        disk.chmod(0o600)
        with disk.open("rb") as stream:
            os.fsync(stream.fileno())
        result = {
            "vm_id": vm_id,
            "pool_id": self.pool_id,
            "vni": vni,
            "size_bytes": disk.stat().st_size,
            "disk_file": filename,
        }
        # Immutable generation manifests let a paused source retain its exact
        # disk after a failed restore elsewhere. Never overwrite its inode.
        with open(path / (filename + ".json"), "x") as stream:
            json.dump(result, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temp = path / "disk.json.tmp"
        with temp.open("w") as stream:
            json.dump(result, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(path / "disk.json")
        path.chmod(0o711)
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return result

    def attach(self, vm_id: str, target: str, expected: dict | None = None) -> dict:
        current = self.descriptor(vm_id)
        filename = (expected or current).get("disk_file", "rootfs.ext4")
        if filename != "rootfs.ext4" and not re.fullmatch(
            r"rootfs-[0-9a-f-]{36}\.ext4", filename
        ):
            raise ValueError("invalid shared disk generation")
        result = self._read_json(self.directory / vm_id / (filename + ".json"))
        if (
            result.get("vm_id") != vm_id
            or result.get("pool_id") != self.pool_id
            or (expected and result != expected)
        ):
            raise ValueError("shared root disk generation mismatch")
        source = self.directory / vm_id / filename
        st = source.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_size != result["size_bytes"]:
            raise ValueError("shared root disk is not the expected regular file")
        fd = os.open(
            target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        os.close(fd)
        _run("mount", "--bind", str(source), target)
        return result


def configured_pool() -> SharedRootfs | None:
    directory = os.environ.get("FLINT_SHARED_ROOTFS_DIR", "")
    return SharedRootfs(directory) if directory else None

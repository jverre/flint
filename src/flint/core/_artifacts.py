"""Cheap copies of disk and memory artifacts.

Rootfs images and memory snapshots are mostly zeros, so every copy keeps them
sparse (and reflinks on filesystems that support it). Read-only artifacts that
many VMs share — golden ``mem``/``vmstate`` — are hard-linked rather than
copied so they share one inode and one page cache. Hard links can't cross
mounts, so those files are mirrored once onto the destination filesystem.
"""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
import subprocess
import threading

from .config import log

_SOURCE_FILE = "source"


def sparse_copy(src: str, dst: str) -> None:
    """Copy ``src`` to ``dst``, reflinking when possible and keeping holes."""
    subprocess.run(["cp", "--reflink=auto", "--sparse=always", src, dst], check=True)


def dig_holes(path: str) -> None:
    """Turn zero-filled ranges of ``path`` into holes (best-effort)."""
    result = subprocess.run(["fallocate", "--dig-holes", path], capture_output=True, text=True)
    if result.returncode != 0:
        log.debug("fallocate --dig-holes %s failed: %s", path, result.stderr.strip())


def fsync_path(path: str) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def shared_readonly(src: str, mirror_root: str, mode: int = 0o444) -> str:
    """Return a root-owned, read-only path with ``src``'s content on ``mirror_root``'s filesystem.

    The result can be hard-linked into VM directories under ``mirror_root``.
    Mirrors are keyed by the source's identity, so regenerating a golden
    snapshot produces a new mirror and running VMs keep their old inode.
    """
    st = os.stat(src)
    key = hashlib.sha1(os.path.abspath(src).encode()).hexdigest()[:12]
    mirror_dir = os.path.join(mirror_root, f"{key}-{st.st_ino}-{st.st_mtime_ns}")
    mirror = os.path.join(mirror_dir, os.path.basename(src))
    if os.path.exists(mirror):
        return mirror

    os.makedirs(mirror_dir, exist_ok=True)
    tmp = f"{mirror}.tmp-{os.getpid()}-{threading.get_ident()}"
    try:
        os.link(src, tmp)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        sparse_copy(src, tmp)
    os.chown(tmp, 0, 0)
    os.chmod(tmp, mode)
    os.replace(tmp, mirror)
    with open(os.path.join(mirror_dir, _SOURCE_FILE), "w") as f:
        f.write(os.path.abspath(src))

    # Drop mirrors of older generations of the same source.
    for name in os.listdir(mirror_root):
        if name.startswith(f"{key}-") and os.path.join(mirror_root, name) != mirror_dir:
            shutil.rmtree(os.path.join(mirror_root, name), ignore_errors=True)
    return mirror


def prune_shared(mirror_root: str) -> None:
    """Remove mirrors whose source was deleted or regenerated.

    Running VMs keep their hard links, so removing a mirror never affects them.
    """
    if not os.path.isdir(mirror_root):
        return
    for name in os.listdir(mirror_root):
        mirror_dir = os.path.join(mirror_root, name)
        try:
            with open(os.path.join(mirror_dir, _SOURCE_FILE)) as f:
                src = f.read()
            st = os.stat(src)
            current = name.endswith(f"-{st.st_ino}-{st.st_mtime_ns}")
        except OSError:
            current = False
        if not current:
            shutil.rmtree(mirror_dir, ignore_errors=True)


def link_into(src: str, dst: str) -> None:
    """Hard-link ``src`` to ``dst``, falling back to a sparse copy across filesystems."""
    try:
        os.link(src, dst)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        log.warning("hard-link %s -> %s crosses filesystems, copying", src, dst)
        sparse_copy(src, dst)

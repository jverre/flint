"""Wipe a sandbox's files before they are deleted.

A sandbox's directory (its jail, or its runtime directory on macOS) holds
everything it wrote: its disk, its memory snapshots, its logs. Before the
directory is removed, every private file in it is discarded: the filesystem
is told to drop its blocks (punching a hole over the whole file), so they go
back to it freed and, on a disk mounted with ``discard``, trimmed. Where
holes aren't supported the data is overwritten with zeros instead. A file
counts as wiped once no data is left in it.

Only regular files with a single link are touched: shared read-only files
(kernel, VMM binary, template snapshots) are hard-linked into each jail,
and are left alone. Symlinks are never followed and other mounts never
entered, so nothing outside the directory can be reached.

This protects data left on the host's disk; it is no substitute for
encrypting that disk.
"""

from __future__ import annotations

import ctypes
import errno
import os
import platform
import shutil
import stat
import struct
import threading

from .config import log

_FALLOC_FL_KEEP_SIZE = 0x01
_FALLOC_FL_PUNCH_HOLE = 0x02
_F_PUNCHHOLE = 99  # macOS fcntl

_libc = ctypes.CDLL(None, use_errno=True)
if platform.system() == "Linux":
    _libc.fallocate.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int64, ctypes.c_int64]

_lock = threading.Lock()
_totals: dict[str, dict[str, int]] = {}  # vm_id -> {"files", "bytes"} wiped so far


def _punch_hole(fd: int, length: int) -> bool:
    if length == 0:
        return True
    if platform.system() == "Linux":
        return _libc.fallocate(fd, _FALLOC_FL_PUNCH_HOLE | _FALLOC_FL_KEEP_SIZE, 0, length) == 0
    if platform.system() == "Darwin":
        import fcntl
        block = os.fstatvfs(fd).f_frsize or 4096
        aligned = -(-length // block) * block
        try:
            fcntl.fcntl(fd, _F_PUNCHHOLE, struct.pack("IIqq", 0, 0, 0, aligned))
            return True
        except OSError:
            return False
    return False


def _has_data(fd: int, length: int) -> bool:
    """True if any part of the file still holds data (not a hole)."""
    if length == 0:
        return False
    try:
        os.lseek(fd, 0, os.SEEK_DATA)
        return True
    except OSError as e:
        if e.errno == errno.ENXIO:
            return False
        return True  # can't tell: assume it does
    except AttributeError:  # no SEEK_DATA here
        return True


def _overwrite(fd: int, length: int) -> None:
    zeros = bytes(1 << 20)
    os.lseek(fd, 0, os.SEEK_SET)
    left = length
    while left > 0:
        left -= os.write(fd, zeros[: min(left, len(zeros))])
    os.fsync(fd)


def wipe_file(path: str) -> int:
    """Discard a private regular file's data in place. Returns the bytes it held."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return 0  # a symlink, a socket, gone, ...
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            return 0
        held = st.st_blocks * 512
        if held == 0:
            return 0
        if not (_punch_hole(fd, st.st_size) and not _has_data(fd, st.st_size)):
            _overwrite(fd, st.st_size)
        return held
    finally:
        os.close(fd)


def wipe_tree(path: str, vm_id: str = "") -> None:
    """Wipe every private file under ``path``, then remove it.

    What was wiped is added to ``vm_id``'s totals (see ``take``).
    """
    if not path or not os.path.lexists(path):
        return
    files = wiped = 0
    try:
        root_dev = os.lstat(path).st_dev
        if os.path.isdir(path) and not os.path.islink(path):
            for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
                # Stay on this filesystem: never wipe inside a mount.
                dirnames[:] = [d for d in dirnames if os.lstat(os.path.join(dirpath, d)).st_dev == root_dev]
                for name in filenames:
                    n = wipe_file(os.path.join(dirpath, name))
                    if n:
                        files += 1
                        wiped += n
        else:
            wiped = wipe_file(path)
            files = 1 if wiped else 0
    except Exception:
        log.exception("wipe of %s failed; removing it anyway", path)
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            os.unlink(path)
        except OSError:
            pass
    if vm_id:
        with _lock:
            t = _totals.setdefault(vm_id, {"files": 0, "bytes": 0})
            t["files"] += files
            t["bytes"] += wiped
    log.debug("wiped %s: %d files, %d bytes", path, files, wiped)


def take(vm_id: str) -> dict[str, int]:
    """What has been wiped for ``vm_id`` since the last call, and forget it."""
    with _lock:
        return _totals.pop(vm_id, {"files": 0, "bytes": 0})

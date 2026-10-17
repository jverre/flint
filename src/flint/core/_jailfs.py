"""Host-side access to paths inside a VMM jail, safe against a hostile VMM.

A jailed VMM owns its chroot directory, so once guest code has run it may
have replaced any entry with a symlink to a host file. The daemon runs as
root: anything it does by path inside a jail must refuse to follow the last
path component. These helpers open with ``O_NOFOLLOW``, check the file type,
and then act on the descriptor rather than the path.
"""

from __future__ import annotations

import os
import socket
import stat
import subprocess


def _open_nofollow(path: str, flags: int, want: tuple[int, ...]) -> int:
    # O_NONBLOCK: opening a FIFO planted by the VMM must not hang the daemon.
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    if stat.S_IFMT(os.fstat(fd).st_mode) not in want:
        os.close(fd)
        raise OSError(f"refusing {path}: unexpected file type")
    return fd


def connect_unix(sock: socket.socket, path: str) -> None:
    """Connect ``sock`` to the Unix socket at ``path`` without following a symlink."""
    fd = _open_nofollow(path, os.O_PATH, (stat.S_IFSOCK,))
    try:
        sock.connect(f"/proc/self/fd/{fd}")
    finally:
        os.close(fd)


def copy_out(src: str, dst: str) -> None:
    """Sparse-copy the regular file at ``src`` (inside a jail) to ``dst``."""
    fd = _open_nofollow(src, os.O_RDONLY, (stat.S_IFREG,))
    try:
        subprocess.run(["cp", "--sparse=always", f"/dev/fd/{fd}", dst], pass_fds=(fd,), check=True)
    finally:
        os.close(fd)


def copy_within(src: str, dst: str, uid: int, gid: int) -> None:
    """Sparse-copy ``src`` to a new private file ``dst``, both inside a jail.

    Whatever is at ``dst`` (possibly a planted symlink) is unlinked, and the new
    file is created exclusively, so the copy can't land on a host file.
    """
    src_fd = _open_nofollow(src, os.O_RDONLY, (stat.S_IFREG,))
    try:
        try:
            os.unlink(dst)
        except FileNotFoundError:
            pass
        dst_fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            os.fchown(dst_fd, uid, gid)
            subprocess.run(
                ["cp", "--sparse=always", f"/dev/fd/{src_fd}", f"/dev/fd/{dst_fd}"],
                pass_fds=(src_fd, dst_fd), check=True,
            )
        finally:
            os.close(dst_fd)
    finally:
        os.close(src_fd)


def own(path: str, uid: int, gid: int, mode: int) -> None:
    """chown + chmod the file or directory at ``path`` itself, never a symlink's target."""
    fd = _open_nofollow(path, os.O_RDONLY, (stat.S_IFREG, stat.S_IFDIR))
    try:
        os.fchown(fd, uid, gid)
        os.fchmod(fd, mode)
    finally:
        os.close(fd)

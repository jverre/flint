"""The daemon's listening sockets: its Unix socket (trusted) and TCP (API token required)."""

from __future__ import annotations

import grp
import os
import socket


def listen_unix(path: str) -> socket.socket:
    """The daemon's Unix socket: only its user, and the ``flint`` group if there
    is one, can connect, and whoever connects is an admin."""
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.connect(path)
        raise SystemExit(f"Another Flint daemon is listening on {path}")
    except (FileNotFoundError, ConnectionRefusedError):
        pass
    finally:
        probe.close()
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old_umask = os.umask(0o177)  # never connectable by others, even briefly
    try:
        sock.bind(path)
    finally:
        os.umask(old_umask)
    try:
        os.chown(path, -1, grp.getgrnam("flint").gr_gid)
        os.chmod(path, 0o660)
    except (KeyError, PermissionError):
        os.chmod(path, 0o600)
    sock.listen(2048)
    sock.set_inheritable(True)
    return sock


def listen_tcp(host: str, port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(2048)
    sock.set_inheritable(True)
    return sock

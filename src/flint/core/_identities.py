"""Persistent, distinct numeric identities for host-side sandbox processes.

IDs are derived from the resource name so a shared disk keeps its owner when
its VM migrates. Collisions fail closed rather than sharing an identity. The
registry survives daemon restarts and is released only after resource cleanup.
No login accounts or supplementary groups are used.
"""
from __future__ import annotations

import grp
from contextlib import closing
import hashlib
import os
from pathlib import Path
import pwd
import re
import sqlite3

from .config import STATE_DIR


def _subordinate_id(uid: int, path: Path) -> bool:
    """Rootless containers can assume IDs delegated in subuid/subgid."""
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return False
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            _account, first, count = line.split(":")
            first, count = int(first), int(count)
            if first < 0 or count <= 0:
                raise ValueError
        except ValueError as exc:
            raise RuntimeError(f"Invalid subordinate identity configuration: {path}") from exc
        if first <= uid < first + count:
            return True
    return False


def identity(owner: str, *, directory: str = STATE_DIR) -> tuple[int, int]:
    if not re.fullmatch(r"[A-Za-z0-9:._-]{1,160}", owner):
        raise ValueError("Invalid workload identity")
    # Stay above ordinary login IDs, below signed uid_t limits, and outside
    # systemd's DynamicUser range. Operators must reserve this numeric range.
    uid = 1_000_000 + int.from_bytes(hashlib.sha256(owner.encode()).digest()[:4], "big") % 1_000_000_000
    for lookup in (pwd.getpwuid, grp.getgrgid):
        try:
            lookup(uid)
        except KeyError:
            continue
        raise RuntimeError(f"Workload identity {uid} conflicts with a host account")
    if any(_subordinate_id(uid, Path(path)) for path in ("/etc/subuid", "/etc/subgid")):
        raise RuntimeError(f"Workload identity {uid} is delegated to a rootless container user")
    Path(directory).mkdir(parents=True, exist_ok=True)
    path = os.path.join(directory, "identities.sqlite")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    with closing(sqlite3.connect(path, timeout=30)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS identities (owner TEXT PRIMARY KEY, uid INTEGER UNIQUE NOT NULL)")
        try:
            db.execute("INSERT INTO identities VALUES (?, ?) ON CONFLICT(owner) DO NOTHING", (owner, uid))
        except sqlite3.IntegrityError as exc:
            raise RuntimeError("Workload identity collision; refusing to share a host identity") from exc
        if db.execute("SELECT uid FROM identities WHERE owner = ?", (owner,)).fetchone()[0] != uid:
            raise RuntimeError("Workload identity configuration changed")
    return uid, uid


def vm_identity(vm_id: str) -> tuple[int, int]:
    return identity("vm:" + vm_id)


def release(owner: str, *, directory: str = STATE_DIR) -> None:
    path = os.path.join(directory, "identities.sqlite")
    if not os.path.exists(path):
        return
    with closing(sqlite3.connect(path, timeout=30)) as db, db:
        db.execute("DELETE FROM identities WHERE owner = ?", (owner,))


def release_kind(kind: str, *, directory: str = STATE_DIR) -> None:
    """Release every identity of a kind (e.g. "proxy") once none can be running."""
    if not re.fullmatch(r"[a-z]+", kind):
        raise ValueError("Invalid workload identity kind")
    path = os.path.join(directory, "identities.sqlite")
    if not os.path.exists(path):
        return
    with closing(sqlite3.connect(path, timeout=30)) as db, db:
        db.execute("DELETE FROM identities WHERE substr(owner, 1, ?) = ?", (len(kind) + 1, kind + ":"))

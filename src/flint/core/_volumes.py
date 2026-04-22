"""Host-side volume management.

Volumes are sparse image files stored under DAEMON_DIR/volumes/. Metadata lives
in the `volumes` SQLite table. In-guest attach (wiring a volume as a block
device in a running or booting VM) is not yet implemented across all backends,
so the current surface is host-side CRUD only: create / list / delete. The
volume screen in the TUI uses this to expose image assets users can manually
attach via backend-specific configuration in the future.
"""

from __future__ import annotations

import os
import sqlite3
import time
import uuid
from dataclasses import dataclass

from .config import DAEMON_DIR, log


VOLUMES_DIR = os.path.join(DAEMON_DIR, "volumes")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS volumes (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    size_gib   INTEGER NOT NULL,
    image_path TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""


@dataclass
class Volume:
    id: str
    name: str
    size_gib: int
    image_path: str
    created_at: float

    def to_dict(self) -> dict:
        size_bytes = 0
        try:
            size_bytes = os.path.getsize(self.image_path)
        except OSError:
            pass
        return {
            "id": self.id,
            "name": self.name,
            "size_gib": self.size_gib,
            "image_path": self.image_path,
            "created_at": self.created_at,
            "size_bytes": size_bytes,
        }


class VolumeStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._conn.executescript(_SCHEMA)
        os.makedirs(VOLUMES_DIR, exist_ok=True)

    def list(self) -> list[Volume]:
        rows = self._conn.execute(
            "SELECT id, name, size_gib, image_path, created_at FROM volumes ORDER BY created_at DESC"
        ).fetchall()
        return [Volume(**dict(r)) for r in rows]

    def get(self, volume_id: str) -> Volume | None:
        row = self._conn.execute(
            "SELECT id, name, size_gib, image_path, created_at FROM volumes WHERE id = ?",
            (volume_id,),
        ).fetchone()
        return Volume(**dict(row)) if row else None

    def get_by_name(self, name: str) -> Volume | None:
        row = self._conn.execute(
            "SELECT id, name, size_gib, image_path, created_at FROM volumes WHERE name = ?",
            (name,),
        ).fetchone()
        return Volume(**dict(row)) if row else None

    def create(self, name: str, size_gib: int) -> Volume:
        if not name or "/" in name:
            raise ValueError("Invalid volume name")
        if size_gib <= 0 or size_gib > 1024:
            raise ValueError("size_gib must be between 1 and 1024")
        if self.get_by_name(name) is not None:
            raise ValueError(f"Volume '{name}' already exists")

        vol_id = uuid.uuid4().hex[:12]
        image_path = os.path.join(VOLUMES_DIR, f"{vol_id}.img")
        # Sparse allocation: create file, truncate to size. No ext4 formatting
        # on host — user formats in-guest after attach via `mkfs.ext4 /dev/vdb`.
        with open(image_path, "wb") as f:
            f.truncate(size_gib * 1024 * 1024 * 1024)

        now = time.time()
        self._conn.execute(
            "INSERT INTO volumes (id, name, size_gib, image_path, created_at) VALUES (?, ?, ?, ?, ?)",
            (vol_id, name, size_gib, image_path, now),
        )
        self._conn.commit()
        log.info("Created volume %s (%s, %d GiB)", vol_id, name, size_gib)
        return Volume(id=vol_id, name=name, size_gib=size_gib, image_path=image_path, created_at=now)

    def delete(self, volume_id: str) -> bool:
        vol = self.get(volume_id)
        if vol is None:
            return False
        try:
            os.unlink(vol.image_path)
        except OSError:
            pass
        self._conn.execute("DELETE FROM volumes WHERE id = ?", (volume_id,))
        self._conn.commit()
        log.info("Deleted volume %s", volume_id)
        return True

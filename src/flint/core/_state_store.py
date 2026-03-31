"""SQLite WAL-mode state store for sandbox durability."""

from __future__ import annotations

import json
import os
import sqlite3
import time

from .config import log
from .types import SandboxState
from ._state_machine import validate_transition

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sandboxes (
    vm_id          TEXT PRIMARY KEY,
    pid            INTEGER NOT NULL,
    vm_dir         TEXT NOT NULL,
    socket_path    TEXT NOT NULL,
    ns_name        TEXT NOT NULL,
    state          TEXT NOT NULL,
    template_id    TEXT NOT NULL DEFAULT 'default',
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL,
    last_health_at REAL,
    timeout_at     REAL,
    timeout_policy TEXT DEFAULT 'kill',
    boot_time_ms   REAL,
    timings_json   TEXT,
    pause_snapshot_dir TEXT,
    daemon_pid     INTEGER,
    error_message  TEXT,
    chroot_base    TEXT,
    backend_kind   TEXT NOT NULL DEFAULT 'linux-firecracker',
    backend_vm_ref TEXT,
    runtime_dir    TEXT,
    guest_arch     TEXT,
    transport_ref  TEXT,
    pause_state_ref TEXT,
    backend_meta_json TEXT
);

CREATE TABLE IF NOT EXISTS state_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    vm_id      TEXT NOT NULL,
    from_state TEXT,
    to_state   TEXT NOT NULL,
    timestamp  REAL NOT NULL,
    daemon_pid INTEGER,
    detail     TEXT
);
"""


class StateStore:
    def __init__(self, db_path: str) -> None:
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        # Migrate existing databases
        try:
            self._conn.execute("ALTER TABLE sandboxes ADD COLUMN chroot_base TEXT")
            self._conn.commit()
        except Exception:
            pass  # Column already exists
        try:
            self._conn.execute("ALTER TABLE sandboxes ADD COLUMN network_policy_json TEXT")
            self._conn.commit()
        except Exception:
            pass  # Column already exists
        for column_def in (
            "backend_kind TEXT NOT NULL DEFAULT 'linux-firecracker'",
            "backend_vm_ref TEXT",
            "runtime_dir TEXT",
            "guest_arch TEXT",
            "transport_ref TEXT",
            "pause_state_ref TEXT",
            "backend_meta_json TEXT",
        ):
            try:
                self._conn.execute(f"ALTER TABLE sandboxes ADD COLUMN {column_def}")
                self._conn.commit()
            except Exception:
                pass

    def insert_sandbox(
        self,
        vm_id: str,
        pid: int,
        vm_dir: str,
        socket_path: str,
        ns_name: str,
        state: SandboxState,
        daemon_pid: int,
        template_id: str = "default",
        boot_time_ms: float | None = None,
        timings_json: dict | None = None,
        chroot_base: str | None = None,
        backend_kind: str = "linux-firecracker",
        backend_vm_ref: str | None = None,
        runtime_dir: str | None = None,
        guest_arch: str | None = None,
        transport_ref: str | None = None,
        pause_state_ref: str | None = None,
        backend_meta_json: dict | None = None,
    ) -> None:
        now = time.time()
        self._conn.execute(
            """INSERT OR REPLACE INTO sandboxes
               (vm_id, pid, vm_dir, socket_path, ns_name, state, template_id,
                created_at, updated_at, boot_time_ms, timings_json, daemon_pid, chroot_base,
                backend_kind, backend_vm_ref, runtime_dir, guest_arch, transport_ref,
                pause_state_ref, backend_meta_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (vm_id, pid, vm_dir, socket_path, ns_name, state.value, template_id,
             now, now, boot_time_ms,
             json.dumps(timings_json) if timings_json else None,
             daemon_pid, chroot_base, backend_kind, backend_vm_ref, runtime_dir,
             guest_arch, transport_ref, pause_state_ref,
             json.dumps(backend_meta_json) if backend_meta_json else None),
        )
        self._conn.execute(
            "INSERT INTO state_log (vm_id, from_state, to_state, timestamp, daemon_pid) VALUES (?, ?, ?, ?, ?)",
            (vm_id, None, state.value, now, daemon_pid),
        )
        self._conn.commit()

    def transition_state(self, vm_id: str, new_state: SandboxState, *, detail: str | None = None) -> None:
        row = self._conn.execute("SELECT state FROM sandboxes WHERE vm_id = ?", (vm_id,)).fetchone()
        if not row:
            log.warning("transition_state: vm_id=%s not found", vm_id)
            return

        from_state = SandboxState(row["state"])
        if not validate_transition(from_state, new_state):
            log.warning("Invalid transition %s -> %s for %s", from_state, new_state, vm_id[:8])
            return

        now = time.time()
        daemon_pid = os.getpid()
        self._conn.execute(
            "UPDATE sandboxes SET state = ?, updated_at = ?, error_message = COALESCE(?, error_message) WHERE vm_id = ?",
            (new_state.value, now, detail, vm_id),
        )
        self._conn.execute(
            "INSERT INTO state_log (vm_id, from_state, to_state, timestamp, daemon_pid, detail) VALUES (?, ?, ?, ?, ?, ?)",
            (vm_id, from_state.value, new_state.value, now, daemon_pid, detail),
        )
        self._conn.commit()

    def update_health(self, vm_id: str, timestamp: float | None = None) -> None:
        now = timestamp or time.time()
        self._conn.execute(
            "UPDATE sandboxes SET last_health_at = ?, updated_at = ? WHERE vm_id = ?",
            (now, now, vm_id),
        )
        self._conn.commit()

    def update_sandbox(self, vm_id: str, **kwargs) -> None:
        """Update arbitrary columns on a sandbox row."""
        if not kwargs:
            return
        cols = ", ".join(f"{k} = ?" for k in kwargs)
        vals = list(kwargs.values())
        vals.append(time.time())
        vals.append(vm_id)
        self._conn.execute(
            f"UPDATE sandboxes SET {cols}, updated_at = ? WHERE vm_id = ?",
            vals,
        )
        self._conn.commit()

    def set_timeout(self, vm_id: str, timeout_at: float, policy: str = "kill") -> None:
        now = time.time()
        self._conn.execute(
            "UPDATE sandboxes SET timeout_at = ?, timeout_policy = ?, updated_at = ? WHERE vm_id = ?",
            (timeout_at, policy, now, vm_id),
        )
        self._conn.commit()

    def set_pause_snapshot(self, vm_id: str, snapshot_dir: str) -> None:
        now = time.time()
        self._conn.execute(
            "UPDATE sandboxes SET pause_snapshot_dir = ?, pause_state_ref = ?, updated_at = ? WHERE vm_id = ?",
            (snapshot_dir, snapshot_dir, now, vm_id),
        )
        self._conn.commit()

    def set_network_policy(self, vm_id: str, policy_json: str) -> None:
        now = time.time()
        self._conn.execute(
            "UPDATE sandboxes SET network_policy_json = ?, updated_at = ? WHERE vm_id = ?",
            (policy_json, now, vm_id),
        )
        self._conn.commit()

    def get_network_policy(self, vm_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT network_policy_json FROM sandboxes WHERE vm_id = ?", (vm_id,)
        ).fetchone()
        return row["network_policy_json"] if row else None

    def get_sandbox(self, vm_id: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM sandboxes WHERE vm_id = ?", (vm_id,)).fetchone()
        return dict(row) if row else None

    def list_active(self) -> list[dict]:
        """Return sandboxes not in Dead state."""
        rows = self._conn.execute(
            "SELECT * FROM sandboxes WHERE state != ?", (SandboxState.DEAD.value,)
        ).fetchall()
        return [dict(r) for r in rows]

    def list_expired(self, now: float | None = None) -> list[dict]:
        """Return sandboxes whose timeout has passed."""
        now = now or time.time()
        rows = self._conn.execute(
            "SELECT * FROM sandboxes WHERE timeout_at IS NOT NULL AND timeout_at < ? AND state NOT IN (?, ?)",
            (now, SandboxState.DEAD.value, SandboxState.PAUSED.value),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_in_state(self, state: SandboxState) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM sandboxes WHERE state = ?", (state.value,)
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_sandbox(self, vm_id: str) -> None:
        self._conn.execute("DELETE FROM sandboxes WHERE vm_id = ?", (vm_id,))
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

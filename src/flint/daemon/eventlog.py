"""The daemon's persisted events and its audit log.

Events: every lifecycle change and action (``vm.created``, ``vm.exec``, ...)
with its ``tenant``, ``vm_id`` and ``actor``, kept for ``EVENT_RETENTION_DAYS``.
They are what ``GET /v1/events`` and ``GET /vms/{id}/timeline`` return;
``/events`` streams them live.

Audit log: one entry per authenticated API call that changes something,
and per sign-in. Never request bodies, so no secret ends up here. Entries
form a hash chain, so a removed or edited entry shows::

    hash = sha256(prev_hash, seq, ts_ms, actor, actor_name, method, path,
                  status, tenant, request_id)   # joined by \\x1f, UTF-8

The first entry's ``prev_hash`` is 64 zeros; after pruning, the oldest kept
entry's ``prev_hash`` anchors the chain.

The ``actor`` is who caused it: ``socket`` (the daemon's Unix socket),
``token:<id>`` (an API token or sign-in session), ``control-plane`` (in a
cluster, unless it says who called it), or ``system`` (the daemon itself).
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import sqlite3
import threading
import time

from flint.core.config import log

EVENT_RETENTION_DAYS = 7
AUDIT_RETENTION_DAYS = 90
GENESIS = "0" * 64

# Who the current request acts as; background work is "system".
actor: contextvars.ContextVar[tuple[str, str]] = contextvars.ContextVar("flint_actor", default=("system", ""))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,
    type       TEXT NOT NULL,
    tenant     TEXT NOT NULL DEFAULT '',
    vm_id      TEXT NOT NULL DEFAULT '',
    actor      TEXT NOT NULL DEFAULT '',
    payload    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_vm ON events (vm_id, seq);
CREATE TABLE IF NOT EXISTS audit (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms      INTEGER NOT NULL,
    actor      TEXT NOT NULL,
    actor_name TEXT NOT NULL DEFAULT '',
    method     TEXT NOT NULL,
    path       TEXT NOT NULL,
    status     INTEGER NOT NULL,
    tenant     TEXT NOT NULL DEFAULT '',
    request_id TEXT NOT NULL DEFAULT '',
    prev_hash  TEXT NOT NULL,
    hash       TEXT NOT NULL
);
"""

AUDIT_FIELDS = ("seq", "ts_ms", "actor", "actor_name", "method", "path", "status", "tenant", "request_id")


def audit_hash(prev_hash: str, entry: dict) -> str:
    """The chain link for ``entry`` after ``prev_hash`` (see the module doc)."""
    parts = [prev_hash] + [str(entry[f]) for f in AUDIT_FIELDS]
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


class EventLog:
    def __init__(self, db_path: str) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._last_prune = 0.0

    # ── Events ───────────────────────────────────────────────────────────

    def record(self, event: dict) -> int:
        """Store an event (which has type, ts and tenant); returns its seq."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO events (ts, type, tenant, vm_id, actor, payload) VALUES (?, ?, ?, ?, ?, ?)",
                (event["ts"], event["type"], event.get("tenant", ""), _vm_id(event), event.get("actor", ""),
                 json.dumps(event, default=str)),
            )
            self._conn.commit()
            seq = cur.lastrowid
        self._maybe_prune()
        return seq

    def query(self, *, since: int = 0, tenants: tuple[str, ...] | None = None, tenant: str = "", vm_id: str = "",
              type: str = "", actor: str = "", limit: int = 500) -> list[dict]:
        """Events after ``since``, oldest first. ``tenants`` limits what the caller may see (None: all)."""
        where, args = ["seq > ?"], [since]
        if tenants is not None:
            where.append(f"tenant IN ({', '.join('?' * len(tenants))})")
            args += list(tenants)
        for col, val in (("tenant", tenant), ("vm_id", vm_id), ("type", type), ("actor", actor)):
            if val:
                where.append(f"{col} = ?")
                args.append(val)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT seq, payload FROM events WHERE {' AND '.join(where)} ORDER BY seq LIMIT ?",
                [*args, max(1, min(limit, 5000))],
            ).fetchall()
        return [{**json.loads(p), "seq": seq} for seq, p in rows]

    # ── Audit ────────────────────────────────────────────────────────────

    def audit(self, *, actor: str, actor_name: str, method: str, path: str, status: int,
              tenant: str = "", request_id: str = "") -> dict:
        """Append an entry to the audit chain."""
        with self._lock:
            row = self._conn.execute("SELECT seq, hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
            prev_seq, prev_hash = row if row else (0, GENESIS)
            entry = {"seq": prev_seq + 1, "ts_ms": int(time.time() * 1000), "actor": actor, "actor_name": actor_name,
                     "method": method, "path": path, "status": int(status), "tenant": tenant, "request_id": request_id}
            entry["prev_hash"], entry["hash"] = prev_hash, audit_hash(prev_hash, entry)
            self._conn.execute(
                f"INSERT INTO audit ({', '.join(AUDIT_FIELDS)}, prev_hash, hash) VALUES ({', '.join('?' * 11)})",
                [entry[f] for f in (*AUDIT_FIELDS, "prev_hash", "hash")],
            )
            self._conn.commit()
        return entry

    def audit_entries(self, since: int = 0, limit: int = 500) -> list[dict]:
        cols = (*AUDIT_FIELDS, "prev_hash", "hash")
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {', '.join(cols)} FROM audit WHERE seq > ? ORDER BY seq LIMIT ?",
                (since, max(1, min(limit, 5000))),
            ).fetchall()
        return [dict(zip(cols, r)) for r in rows]

    def verify_audit(self) -> dict:
        """Recompute the whole chain. Reports the first entry that doesn't hold."""
        prev, count, since = None, 0, 0
        while True:
            batch = self.audit_entries(since, 5000)
            for e in batch:
                if prev is not None and (e["prev_hash"] != prev["hash"] or e["seq"] != prev["seq"] + 1):
                    return {"ok": False, "count": count, "broken_at": e["seq"], "why": "doesn't follow the entry before it"}
                if audit_hash(e["prev_hash"], e) != e["hash"]:
                    return {"ok": False, "count": count, "broken_at": e["seq"], "why": "hash doesn't match its fields"}
                prev, count = e, count + 1
            if len(batch) < 5000:
                return {"ok": True, "count": count, "last_hash": prev["hash"] if prev else GENESIS}
            since = batch[-1]["seq"]

    # ── Retention ────────────────────────────────────────────────────────

    def _maybe_prune(self) -> None:
        now = time.time()
        if now - self._last_prune < 3600:
            return
        self._last_prune = now
        with self._lock:
            self._conn.execute("DELETE FROM events WHERE ts < ?", (now - EVENT_RETENTION_DAYS * 86400,))
            # Keep the last link as an anchor even after a long idle period,
            # so sequence numbers and the hash chain never restart at one.
            self._conn.execute("DELETE FROM audit WHERE ts_ms < ? AND seq < (SELECT MAX(seq) FROM audit)",
                               (int((now - AUDIT_RETENTION_DAYS * 86400) * 1000),))
            self._conn.commit()
        log.debug("eventlog: pruned events older than %d days, audit older than %d", EVENT_RETENTION_DAYS, AUDIT_RETENTION_DAYS)


def _vm_id(event: dict) -> str:
    if event.get("vm_id"):
        return str(event["vm_id"])
    vm = event.get("vm")
    return str(vm.get("vm_id", "")) if isinstance(vm, dict) else ""

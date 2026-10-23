"""Per-tenant quotas on a single daemon, checked when a VM or volume is created.

A tenant can be limited in VMs, vCPUs, memory and volume space. Every VM
counts, paused ones too: they can be resumed at any time. 0 means no limit.
Creates in flight are reserved, so concurrent creates can't overshoot.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tenant_quotas (
    tenant         TEXT PRIMARY KEY,
    max_vms        INTEGER NOT NULL DEFAULT 0,
    max_vcpus      INTEGER NOT NULL DEFAULT 0,
    max_memory_mib INTEGER NOT NULL DEFAULT 0,
    max_volume_gib INTEGER NOT NULL DEFAULT 0,
    updated_at     REAL NOT NULL
);
"""

LIMITS = ("max_vms", "max_vcpus", "max_memory_mib", "max_volume_gib")


class QuotaExceeded(Exception):
    """The tenant is at a limit. The API answers 429, like the cluster."""


@dataclass
class Usage:
    vms: int = 0
    vcpus: int = 0
    memory_mib: int = 0
    volume_gib: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(self.vms + other.vms, self.vcpus + other.vcpus,
                     self.memory_mib + other.memory_mib, self.volume_gib + other.volume_gib)


def over(limits: dict[str, int], used: Usage, want: Usage) -> str | None:
    """Why ``want`` more doesn't fit the limits next to ``used``, or None if it does."""
    for limit, field, unit in (("max_vms", "vms", "VMs"), ("max_vcpus", "vcpus", "vCPUs"),
                               ("max_memory_mib", "memory_mib", "MiB of memory"),
                               ("max_volume_gib", "volume_gib", "GiB of volumes")):
        cap, have, more = limits.get(limit, 0), getattr(used, field), getattr(want, field)
        if cap and more and have + more > cap:
            return f"uses {have} of its {cap} {unit} and this needs {more} more"
    return None


class Quotas:
    def __init__(self, db_path: str) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._pending: dict[str, Usage] = {}

    def get(self, tenant: str) -> dict[str, int]:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {', '.join(LIMITS)} FROM tenant_quotas WHERE tenant = ?", (tenant,)
            ).fetchone()
        return dict(zip(LIMITS, row)) if row else dict.fromkeys(LIMITS, 0)

    def set(self, tenant: str, changes: dict) -> dict[str, int]:
        """Change some limits (the others stay). Values are integers >= 0."""
        for k, v in changes.items():
            if k not in LIMITS:
                raise ValueError(f"unknown limit {k!r}; limits are {', '.join(LIMITS)}")
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                raise ValueError(f"{k} must be an integer >= 0 (0 = no limit)")
        limits = {**self.get(tenant), **changes}
        with self._lock:
            self._conn.execute(
                f"INSERT INTO tenant_quotas (tenant, {', '.join(LIMITS)}, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
                f"ON CONFLICT (tenant) DO UPDATE SET "
                + ", ".join(f"{k} = excluded.{k}" for k in LIMITS) + ", updated_at = excluded.updated_at",
                (tenant, *(limits[k] for k in LIMITS), time.time()),
            )
            self._conn.commit()
        return limits

    @contextmanager
    def reserve(self, tenant: str, used: Usage, want: Usage):
        """Hold ``want`` for ``tenant`` while it's created, or raise QuotaExceeded.

        ``used`` is what the tenant has now; creates still in flight count too.
        """
        limits = self.get(tenant)
        with self._lock:
            pending = self._pending.get(tenant, Usage())
            why = over(limits, used + pending, want)
            if why:
                raise QuotaExceeded(f"Tenant '{tenant}' is at its quota: it {why}")
            self._pending[tenant] = pending + want
        try:
            yield
        finally:
            with self._lock:
                left = self._pending[tenant] + Usage(-want.vms, -want.vcpus, -want.memory_mib, -want.volume_gib)
                if left == Usage():
                    self._pending.pop(tenant, None)
                else:
                    self._pending[tenant] = left

    def view(self, tenant: str, used: Usage) -> dict:
        return {"tenant": tenant, **self.get(tenant), **asdict(used)}

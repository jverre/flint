"""Crash recovery engine — reclaims running sandboxes after daemon restart."""

from __future__ import annotations

import os

from .config import log
from .types import SandboxState

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ._state_store import StateStore
    from .manager import SandboxManager


class RecoveryReport:
    def __init__(self) -> None:
        self.reclaimed = 0
        self.dead_cleaned = 0
        self.paused_kept = 0

    def __str__(self) -> str:
        return f"{self.reclaimed} running reclaimed, {self.dead_cleaned} dead cleaned, {self.paused_kept} paused preserved"


class RecoveryEngine:
    def __init__(self, store: StateStore, manager: SandboxManager) -> None:
        self._store = store
        self._manager = manager

    def recover(self) -> RecoveryReport:
        report = RecoveryReport()
        active = self._store.list_active()

        if not active:
            log.debug("Recovery: no active sandboxes to recover")
            return report

        log.info("Recovery: found %d active sandbox(es) to probe", len(active))
        daemon_pid = os.getpid()

        for row in active:
            vm_id = row["vm_id"]
            backend_kind = row.get("backend_kind") or self._manager.default_kind
            try:
                backend = self._manager.backend_for(backend_kind)
            except Exception:
                log.warning("[%s] backend %s unavailable; marking dead", vm_id[:8], backend_kind)
                self._store.transition_state(vm_id, SandboxState.DEAD, detail=f"backend {backend_kind} unavailable")
                report.dead_cleaned += 1
                continue

            probe, entry = backend.recover(row)

            if probe == "alive" and entry is not None:
                with self._manager._lock:
                    self._manager._sandboxes[vm_id] = entry
                self._store.update_sandbox(vm_id, daemon_pid=daemon_pid)
                report.reclaimed += 1
            elif probe == "paused":
                self._store.update_sandbox(vm_id, daemon_pid=daemon_pid)
                report.paused_kept += 1
            else:
                self._store.transition_state(vm_id, SandboxState.DEAD, detail="cleaned up during recovery")
                report.dead_cleaned += 1

        log.info("Recovery: %s", report)
        return report

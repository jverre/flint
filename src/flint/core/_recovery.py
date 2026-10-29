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
        backend = self._manager.backend
        backend.prepare_recovery(active)

        for row in active:
            vm_id = row.vm_id
            if row.state in (SandboxState.DELETING, SandboxState.ERROR):
                # Cleanup intent survives restart. Never reclaim it as serving.
                try:
                    self._manager.kill(vm_id)
                    report.dead_cleaned += 1
                except Exception:
                    log.exception("[%s] deletion retained for retry during recovery", vm_id[:8])
                continue
            probe, entry = backend.recover_row(row)

            if probe == "alive" and entry is not None:
                try:
                    backend.confine_recovered(row)
                except Exception:
                    log.exception("[%s] recovered VM could not be confined", vm_id[:8])
                    self._store.transition_state(vm_id, SandboxState.DELETING, detail="recovery enforcement failed")
                    try:
                        self._manager.kill(vm_id)
                    except Exception:
                        log.exception("[%s] deletion retained for retry during recovery", vm_id[:8])
                    continue
                self._manager.track(entry)
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

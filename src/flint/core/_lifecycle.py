"""Lifecycle manager — timeout enforcement and error cleanup."""

from __future__ import annotations

import threading
import time

from .config import log
from .types import SandboxState

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ._state_store import StateStore
    from .manager import SandboxManager


class LifecycleManager:
    def __init__(
        self,
        state_store: StateStore | None,
        manager: SandboxManager | None,
        interval: float = 1.0,
        error_cleanup_delay: float = 60.0,
    ) -> None:
        self._store = state_store
        self._manager = manager
        self._interval = interval
        self._error_cleanup_delay = error_cleanup_delay
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="lifecycle-mgr")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self._interval + 1)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._enforce_timeouts()
                self._cleanup_errors()
            except Exception:
                log.exception("LifecycleManager error")

    def _enforce_timeouts(self) -> None:
        if not self._store or not self._manager:
            return

        now = time.time()
        expired = self._store.list_expired(now)
        for row in expired:
            vm_id = row["vm_id"]
            policy = row.get("timeout_policy", "kill")
            sid = vm_id[:8]

            try:
                if policy == "pause":
                    log.info("[%s] timeout — pausing (policy=%s)", sid, policy)
                    self._manager.pause(vm_id)
                else:
                    log.info("[%s] timeout — killing (policy=%s)", sid, policy)
                    self._manager.kill(vm_id)
            except Exception:
                log.exception("[%s] timeout enforcement failed", sid)

    def _cleanup_errors(self) -> None:
        if not self._store or not self._manager:
            return

        now = time.time()
        error_rows = self._store.list_in_state(SandboxState.ERROR)
        for row in error_rows:
            vm_id = row["vm_id"]
            updated_at = row["updated_at"]
            if now - updated_at >= self._error_cleanup_delay:
                sid = vm_id[:8]
                log.info("[%s] error cleanup — killing after %.0fs", sid, now - updated_at)
                try:
                    self._manager.kill(vm_id)
                except Exception:
                    # If kill fails, just mark as dead
                    self._store.transition_state(vm_id, SandboxState.DEAD, detail="error cleanup failed")

"""Health monitor — background thread that probes sandbox liveness."""

from __future__ import annotations

import threading
import time

from .config import log
from .types import SandboxState

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ._state_store import StateStore
    from .manager import SandboxManager


class HealthMonitor:
    def __init__(
        self,
        state_store: StateStore | None,
        manager: SandboxManager | None,
        interval: float = 5.0,
    ) -> None:
        self._store = state_store
        self._manager = manager
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="health-monitor")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self._interval + 1)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._check_all()
            except Exception:
                log.exception("HealthMonitor error")

    def _check_all(self) -> None:
        if not self._manager:
            return

        now = time.time()
        for entry in self._manager.entries():
            vm_id = entry.vm_id
            if entry.state != SandboxState.RUNNING:
                continue

            alive, detail = self._manager.backend.check_entry_alive(entry)

            if alive:
                if self._store:
                    self._store.update_health(vm_id, now)
            else:
                with self._manager._lock:
                    # A deletion may have started during the (slow) probe.
                    if entry.state != SandboxState.RUNNING:
                        continue
                    entry.state = SandboxState.ERROR
                log.warning("[%s] sandbox unhealthy — transitioning to error", vm_id[:8])
                # Outside the lock: publishing looks the VM up. The store
                # rejects DELETING -> ERROR if a deletion wins from here.
                if self._store:
                    self._store.transition_state(
                        vm_id, SandboxState.ERROR,
                        detail=detail or "sandbox unhealthy",
                    )

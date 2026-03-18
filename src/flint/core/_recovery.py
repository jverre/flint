"""Crash recovery engine — reclaims running VMs after daemon restart."""

from __future__ import annotations

import os

from .config import log, GUEST_IP, AGENT_PORT, DEFAULT_TEMPLATE_ID
from .types import _SandboxEntry, SandboxState
from ._boot import _RecoveredProcess
from ._netns import _delete_netns
from ._firecracker import _fc_request, _wait_for_agent
from ._jailer import cleanup_jailer

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


def _probe_sandbox(row: dict) -> str:
    """Probe actual state of a sandbox. Returns 'alive', 'dead', or 'paused'."""
    state = row["state"]

    if state == SandboxState.PAUSED.value:
        pause_dir = row.get("pause_snapshot_dir")
        if pause_dir and os.path.exists(f"{pause_dir}/pause-vmstate"):
            return "paused"
        return "dead"

    pid = row["pid"]
    try:
        os.kill(pid, 0)  # signal 0 = existence check
    except ProcessLookupError:
        return "dead"
    except PermissionError:
        pass  # process exists but different user

    socket_path = row["socket_path"]
    if not os.path.exists(socket_path):
        return "dead"

    try:
        resp = _fc_request(socket_path, "GET", "/", {})
        if "HTTP/1.1" in resp:
            return "alive"
        return "dead"
    except Exception:
        return "dead"


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
            probe = _probe_sandbox(row)

            if probe == "alive":
                self._reclaim(row, daemon_pid)
                report.reclaimed += 1
            elif probe == "paused":
                self._store.update_sandbox(vm_id, daemon_pid=daemon_pid)
                report.paused_kept += 1
            else:  # dead
                self._cleanup_dead(row)
                report.dead_cleaned += 1

        log.info("Recovery: %s", report)
        return report

    def _reclaim(self, row: dict, daemon_pid: int) -> None:
        """Reclaim a still-alive sandbox."""
        vm_id = row["vm_id"]
        pid = row["pid"]
        ns_name = row["ns_name"]
        vm_dir = row["vm_dir"]
        sid = vm_id[:8]

        try:
            agent_url = _wait_for_agent(ns_name, retries=50)
        except Exception:
            log.warning("[%s] reclaim: agent health check failed, treating as dead", sid)
            self._cleanup_dead(row)
            return

        process_shim = _RecoveredProcess(pid)
        entry = _SandboxEntry(
            vm_id=vm_id,
            process=process_shim,
            pid=pid,
            vm_dir=vm_dir,
            socket_path=row["socket_path"],
            ns_name=ns_name,
            guest_ip=GUEST_IP,
            agent_url=agent_url,
            agent_healthy=True,
            state=SandboxState.RUNNING,
            template_id=row.get("template_id", DEFAULT_TEMPLATE_ID),
            chroot_base=row.get("chroot_base") or "",
        )

        with self._manager._lock:
            self._manager._sandboxes[vm_id] = entry

        self._store.update_sandbox(vm_id, daemon_pid=daemon_pid)
        log.info("[%s] reclaimed (pid=%d)", sid, pid)

    def _cleanup_dead(self, row: dict) -> None:
        """Clean up a dead sandbox's orphaned resources."""
        vm_id = row["vm_id"]
        pid = row["pid"]
        ns_name = row["ns_name"]
        chroot_base = row.get("chroot_base") or ""
        sid = vm_id[:8]

        # Kill zombie process if still exists
        try:
            os.kill(pid, 9)
        except (ProcessLookupError, PermissionError):
            pass

        # Clean up netns
        try:
            _delete_netns(ns_name)
        except Exception:
            pass

        # Clean up jailer chroot and cgroups
        if chroot_base:
            cleanup_jailer(chroot_base, vm_id)

        self._store.transition_state(vm_id, SandboxState.DEAD, detail="cleaned up during recovery")
        log.info("[%s] dead - cleaned up", sid)

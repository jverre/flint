from __future__ import annotations

import os
import socket
import threading
import time
from typing import TYPE_CHECKING

from .config import log, GOLDEN_DIR, GOLDEN_TAP, GUEST_IP, DEFAULT_TEMPLATE_ID
from .types import _SandboxEntry, SandboxState
from ._boot import _boot_from_snapshot, _teardown_vm, BootResult
from ._snapshot import golden_snapshot_exists
from ._template_registry import template_snapshot_exists as _template_snapshot_exists
from ._tcp import _read_tcp_output

if TYPE_CHECKING:
    from ._state_store import StateStore


class SandboxManager:
    """Owns all sandbox state and lifecycle. No TUI dependencies."""

    def __init__(self, state_store: StateStore | None = None) -> None:
        self._sandboxes: dict[str, _SandboxEntry] = {}
        self._lock = threading.Lock()
        self._state_store = state_store

    def create(self, *, template_id: str = DEFAULT_TEMPLATE_ID, allow_internet_access: bool = True, use_pool: bool = True, use_pyroute2: bool = True) -> str:
        """Start an interactive VM from a template snapshot. Returns the vm_id."""
        if template_id == DEFAULT_TEMPLATE_ID:
            if not golden_snapshot_exists():
                raise RuntimeError(f"Golden snapshot not found in {GOLDEN_DIR}")
        else:
            if not _template_snapshot_exists(template_id):
                raise RuntimeError(f"Template snapshot not found: {template_id}")

        boot = _boot_from_snapshot(
            template_id=template_id,
            allow_internet_access=allow_internet_access,
            use_pool=use_pool,
            use_pyroute2=use_pyroute2,
            network_overrides=[{"iface_id": "eth0", "host_dev_name": GOLDEN_TAP}],
        )

        vm_id = boot.vm_id
        sock = boot.tcp_socket

        # Send initial command to measure time-to-interactive
        t0 = time.monotonic()
        sock.sendall(b'echo benchmark\n')
        sock.settimeout(5.0)
        try:
            sock.recv(4096)
        except socket.timeout:
            pass
        sock.settimeout(None)
        boot.timings["exec_command_ms"] = (time.monotonic() - t0) * 1000

        entry = _SandboxEntry(
            vm_id=vm_id,
            process=boot.process,
            pid=boot.process.pid,
            vm_dir=boot.vm_dir,
            socket_path=boot.socket_path,
            ns_name=boot.ns_name,
            guest_ip=GUEST_IP,
            tcp_socket=sock,
            tcp_connected=True,
            state=SandboxState.RUNNING,
            template_id=template_id,
            t_instance_start=boot.t_total,
            ready_time_ms=(time.monotonic() - boot.t_total) * 1000,
            timings=boot.timings,
        )

        with self._lock:
            self._sandboxes[vm_id] = entry

        # Persist to state store if available
        if self._state_store:
            self._state_store.insert_sandbox(
                vm_id=vm_id,
                pid=boot.process.pid,
                vm_dir=boot.vm_dir,
                socket_path=boot.socket_path,
                ns_name=boot.ns_name,
                state=SandboxState.RUNNING,
                daemon_pid=os.getpid(),
                template_id=template_id,
                boot_time_ms=entry.ready_time_ms,
                timings_json=boot.timings,
            )

        threading.Thread(
            target=_read_tcp_output,
            args=(sock, entry.dispatch_output, lambda: self._on_disconnect(vm_id)),
            daemon=True,
        ).start()

        total_ms = (time.monotonic() - boot.t_total) * 1000
        parts = " | ".join(f"{k}={v:.1f}" for k, v in boot.timings.items())
        log.debug("[%s] DONE %.0f ms: %s", vm_id[:8], total_ms, parts)

        return vm_id

    def kill(self, sandbox_id: str) -> None:
        with self._lock:
            entry = self._sandboxes.pop(sandbox_id, None)
        if not entry:
            return

        if entry.tcp_socket:
            try:
                entry.tcp_socket.close()
            except OSError:
                pass
        _teardown_vm(entry.process, entry.ns_name, entry.vm_dir)

        if self._state_store:
            self._state_store.transition_state(sandbox_id, SandboxState.DEAD)

    def pause(self, sandbox_id: str) -> None:
        """Pause a running sandbox: snapshot state to disk, kill process."""
        from ._firecracker import _fc_patch, _fc_put, _fc_status_ok

        with self._lock:
            entry = self._sandboxes.get(sandbox_id)
        if not entry:
            raise RuntimeError(f"Sandbox {sandbox_id} not found")
        if entry.state != SandboxState.RUNNING:
            raise RuntimeError(f"Sandbox {sandbox_id} is not running (state={entry.state})")

        # 1. Pause vCPU
        _fc_patch(entry.socket_path, "/vm", {"state": "Paused"})

        # 2. Create pause snapshot
        snapshot_body = {
            "snapshot_type": "Full",
            "snapshot_path": f"{entry.vm_dir}/pause-vmstate",
            "mem_file_path": f"{entry.vm_dir}/pause-mem",
        }
        resp = _fc_put(entry.socket_path, "/snapshot/create", snapshot_body)
        if not _fc_status_ok(resp):
            # Resume VM on failure
            _fc_patch(entry.socket_path, "/vm", {"state": "Resumed"})
            raise RuntimeError(f"Snapshot create failed: {resp}")

        # 3. Close TCP socket
        if entry.tcp_socket:
            try:
                entry.tcp_socket.close()
            except OSError:
                pass

        # 4. Kill Firecracker process (snapshot is on disk)
        if entry.process:
            entry.process.kill()
            try:
                entry.process.wait(timeout=2)
            except Exception:
                pass

        # 5. Update state
        entry.state = SandboxState.PAUSED
        entry.tcp_connected = False
        entry.tcp_socket = None
        entry.process = None

        # 6. Persist
        if self._state_store:
            self._state_store.transition_state(sandbox_id, SandboxState.PAUSED)
            self._state_store.set_pause_snapshot(sandbox_id, entry.vm_dir)

        # 7. Remove from in-memory dict (no active process to track)
        with self._lock:
            self._sandboxes.pop(sandbox_id, None)

        log.debug("[%s] paused", sandbox_id[:8])

    def resume(self, sandbox_id: str) -> str:
        """Resume a paused sandbox from its snapshot."""
        from ._firecracker import _fc_put, _fc_patch, _fc_status_ok, _wait_for_api_socket, _tcp_connect
        from ._netns import _popen_in_ns
        import subprocess as _subprocess

        if not self._state_store:
            raise RuntimeError("StateStore required for resume")

        row = self._state_store.get_sandbox(sandbox_id)
        if not row:
            raise RuntimeError(f"Sandbox {sandbox_id} not found in state store")
        if row["state"] != SandboxState.PAUSED.value:
            raise RuntimeError(f"Sandbox {sandbox_id} is not paused (state={row['state']})")

        vm_dir = row["vm_dir"]
        ns_name = row["ns_name"]
        socket_path = row["socket_path"]

        # 1. Start new Firecracker in existing netns
        log_path = f"{vm_dir}/firecracker.log"
        with open(log_path, "w") as log_fd:
            process = _popen_in_ns(
                ns_name,
                ["firecracker", "--api-sock", socket_path, "--id", sandbox_id],
                stdin=_subprocess.DEVNULL, stdout=log_fd, stderr=_subprocess.STDOUT,
                start_new_session=True,
            )

        try:
            _wait_for_api_socket(socket_path)

            # 2. Load pause snapshot (not golden)
            snapshot_body = {
                "snapshot_path": f"{vm_dir}/pause-vmstate",
                "mem_backend": {"backend_type": "File", "backend_path": f"{vm_dir}/pause-mem"},
                "enable_diff_snapshots": False,
                "resume_vm": False,
            }
            resp = _fc_put(socket_path, "/snapshot/load", snapshot_body)
            if not _fc_status_ok(resp):
                raise RuntimeError(f"snapshot/load failed: {resp}")

            # 3. Patch drives
            rootfs_path = f"{vm_dir}/rootfs.ext4"
            _fc_patch(socket_path, "/drives/rootfs", {"drive_id": "rootfs", "path_on_host": rootfs_path})

            # 4. Resume VM
            _fc_patch(socket_path, "/vm", {"state": "Resumed"})

            # 5. Reconnect TCP
            tcp_sock = _tcp_connect(ns_name)
        except Exception:
            process.kill()
            try:
                process.wait(timeout=2)
            except Exception:
                pass
            raise

        # 6. Create entry and insert into in-memory dict
        entry = _SandboxEntry(
            vm_id=sandbox_id,
            process=process,
            pid=process.pid,
            vm_dir=vm_dir,
            socket_path=socket_path,
            ns_name=ns_name,
            guest_ip=GUEST_IP,
            tcp_socket=tcp_sock,
            tcp_connected=True,
            state=SandboxState.RUNNING,
            template_id=row.get("template_id", DEFAULT_TEMPLATE_ID),
        )

        with self._lock:
            self._sandboxes[sandbox_id] = entry

        # 7. Persist
        if self._state_store:
            self._state_store.transition_state(sandbox_id, SandboxState.RUNNING)
            self._state_store.update_sandbox(sandbox_id, pid=process.pid, daemon_pid=os.getpid())

        # 8. Start TCP output reader
        threading.Thread(
            target=_read_tcp_output,
            args=(tcp_sock, entry.dispatch_output, lambda: self._on_disconnect(sandbox_id)),
            daemon=True,
        ).start()

        log.debug("[%s] resumed", sandbox_id[:8])
        return sandbox_id

    def set_timeout(self, sandbox_id: str, timeout_seconds: float, policy: str = "kill") -> None:
        """Set or update the timeout for a sandbox."""
        if not self._state_store:
            raise RuntimeError("StateStore required for set_timeout")
        timeout_at = time.time() + timeout_seconds
        self._state_store.set_timeout(sandbox_id, timeout_at, policy)

    def list_dicts(self) -> list[dict]:
        """Return JSON-serializable dicts for all VMs."""
        with self._lock:
            return [entry.to_dict() for entry in self._sandboxes.values()]

    def get_dict(self, sandbox_id: str) -> dict | None:
        """Return JSON-serializable dict for a single VM, or None."""
        with self._lock:
            entry = self._sandboxes.get(sandbox_id)
        if entry is None:
            return None
        return entry.to_dict()

    def get_entry(self, sandbox_id: str) -> _SandboxEntry | None:
        """Return the raw entry for a VM (for subscribe/send_raw). None if not found."""
        with self._lock:
            return self._sandboxes.get(sandbox_id)

    def vm_ids(self) -> list[str]:
        """Return list of all VM IDs."""
        with self._lock:
            return list(self._sandboxes.keys())

    def _on_disconnect(self, sandbox_id: str) -> None:
        with self._lock:
            entry = self._sandboxes.get(sandbox_id)
            if entry:
                entry.tcp_connected = False

from __future__ import annotations

import socket
import threading
import time

from .config import log, GOLDEN_DIR, GOLDEN_TAP, GUEST_IP, DEFAULT_TEMPLATE_ID
from .types import _SandboxEntry
from ._boot import _boot_from_snapshot, _teardown_vm
from ._snapshot import golden_snapshot_exists
from ._template_registry import template_snapshot_exists as _template_snapshot_exists
from ._tcp import _read_tcp_output


class SandboxManager:
    """Owns all sandbox state and lifecycle. No TUI dependencies."""

    def __init__(self) -> None:
        self._sandboxes: dict[str, _SandboxEntry] = {}
        self._lock = threading.Lock()

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

        vm_id = boot["vm_id"]
        sock = boot["tcp_socket"]

        # Send initial command to measure time-to-interactive
        t0 = time.monotonic()
        sock.sendall(b'echo benchmark\n')
        sock.settimeout(5.0)
        try:
            sock.recv(4096)
        except socket.timeout:
            pass
        sock.settimeout(None)
        boot["timings"]["exec_command_ms"] = (time.monotonic() - t0) * 1000

        entry = _SandboxEntry(
            vm_id=vm_id,
            process=boot["process"],
            pid=boot["process"].pid,
            vm_dir=boot["vm_dir"],
            socket_path=boot["socket_path"],
            ns_name=boot["ns_name"],
            guest_ip=GUEST_IP,
            tcp_socket=sock,
            tcp_connected=True,
            state="Started",
            template_id=template_id,
            t_instance_start=boot["t_total"],
            ready_time_ms=(time.monotonic() - boot["t_total"]) * 1000,
            timings=boot["timings"],
        )

        with self._lock:
            self._sandboxes[vm_id] = entry

        threading.Thread(
            target=_read_tcp_output,
            args=(sock, entry.dispatch_output, lambda: self._on_disconnect(vm_id)),
            daemon=True,
        ).start()

        total_ms = (time.monotonic() - boot["t_total"]) * 1000
        parts = " | ".join(f"{k}={v:.1f}" for k, v in boot["timings"].items())
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

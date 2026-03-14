from __future__ import annotations

import threading
from typing import Callable

import httpx
import websockets.sync.client as ws_sync

from flint.core.config import DAEMON_URL

DEFAULT_URL = DAEMON_URL


class _TerminalConnection:
    """Manages a WebSocket connection to a VM terminal."""

    def __init__(self, ws_url: str, on_output: Callable[[bytes], None]) -> None:
        self._ws = ws_sync.connect(ws_url)
        self._on_output = on_output
        self._closed = False
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()

    def send(self, data: bytes) -> None:
        if not self._closed:
            try:
                self._ws.send(data)
            except Exception:
                pass

    def close(self) -> None:
        self._closed = True
        try:
            self._ws.close()
        except Exception:
            pass

    def _read_loop(self) -> None:
        try:
            for message in self._ws:
                if self._closed:
                    break
                if isinstance(message, bytes):
                    self._on_output(message)
                elif isinstance(message, str):
                    self._on_output(message.encode())
        except Exception:
            pass


class DaemonClient:
    def __init__(self, base_url: str = DEFAULT_URL) -> None:
        self._base_url = base_url
        self._ws_base_url = base_url.replace("http://", "ws://").replace("https://", "wss://")
        self._http = httpx.Client(base_url=base_url, timeout=30.0)
        self._terminals: dict[str, _TerminalConnection] = {}

    def close(self) -> None:
        for conn in self._terminals.values():
            conn.close()
        self._terminals.clear()
        self._http.close()

    def create(self, *, template_id: str = "default", allow_internet_access: bool = True, use_pool: bool = True, use_pyroute2: bool = True) -> dict:
        resp = self._http.post("/vms", params={"template_id": template_id, "allow_internet_access": allow_internet_access, "use_pool": use_pool, "use_pyroute2": use_pyroute2})
        resp.raise_for_status()
        return resp.json()["vm"]

    def kill(self, vm_id: str) -> None:
        self.disconnect_terminal(vm_id)
        resp = self._http.delete(f"/vms/{vm_id}")
        resp.raise_for_status()

    def list(self) -> list[dict]:
        resp = self._http.get("/vms")
        resp.raise_for_status()
        return resp.json()["vms"]

    def get(self, vm_id: str) -> dict | None:
        resp = self._http.get(f"/vms/{vm_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()["vm"]

    def connect_terminal(self, vm_id: str, on_output: Callable[[bytes], None]) -> None:
        """Open WebSocket to VM terminal. Calls on_output with binary data from VM."""
        if vm_id in self._terminals:
            self.disconnect_terminal(vm_id)
        ws_url = f"{self._ws_base_url}/vms/{vm_id}/terminal"
        self._terminals[vm_id] = _TerminalConnection(ws_url, on_output)

    def send_input(self, vm_id: str, data: bytes) -> None:
        conn = self._terminals.get(vm_id)
        if conn:
            conn.send(data)

    def disconnect_terminal(self, vm_id: str) -> None:
        conn = self._terminals.pop(vm_id, None)
        if conn:
            conn.close()

    # ── Template methods ───────────────────────────────────────────────────

    def build_template(self, name: str, dockerfile: str, rootfs_size_mb: int = 500) -> dict:
        resp = self._http.post(
            "/templates/build",
            json={"name": name, "dockerfile": dockerfile, "rootfs_size_mb": rootfs_size_mb},
            timeout=600.0,
        )
        resp.raise_for_status()
        return resp.json()

    def list_templates(self) -> list[dict]:
        resp = self._http.get("/templates")
        resp.raise_for_status()
        return resp.json()["templates"]

    def get_template(self, template_id: str) -> dict | None:
        resp = self._http.get(f"/templates/{template_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()["template"]

    def delete_template(self, template_id: str) -> None:
        resp = self._http.delete(f"/templates/{template_id}")
        resp.raise_for_status()

    @staticmethod
    def is_daemon_running(base_url: str = DEFAULT_URL) -> bool:
        try:
            resp = httpx.get(f"{base_url}/health", timeout=1.0)
            return resp.status_code == 200
        except Exception:
            return False

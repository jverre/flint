from __future__ import annotations

import threading
import time
import uuid
from typing import Callable

import httpx

from flint.core.config import API_KEY, DAEMON_URL, TENANT
from flint._client.transport import http_client, ws_connect

DEFAULT_URL = DAEMON_URL


def auth_headers(api_key: str = API_KEY) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


class _TerminalConnection:
    """Manages a WebSocket connection to a VM terminal."""

    def __init__(self, base_url: str, path: str, on_output: Callable[[bytes], None], headers: dict[str, str] | None = None) -> None:
        self._ws = ws_connect(base_url, path, headers)
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


def _raise_for_status(resp: httpx.Response) -> None:
    """Like raise_for_status, with the daemon's error detail in the message."""
    if resp.is_success:
        return
    try:
        detail = resp.json().get("detail", resp.text)
    except ValueError:
        detail = resp.text
    raise httpx.HTTPStatusError(f"{resp.status_code}: {detail}", request=resp.request, response=resp)


# Answers worth retrying with the same Idempotency-Key: the control plane
# couldn't tell (a replica died behind the load balancer) or the VM was busy.
_RETRY_STATUS = {502, 503, 504}
_RETRY_DETAIL = ("in progress",)


class DaemonClient:
    def __init__(self, base_url: str = DEFAULT_URL, api_key: str = API_KEY, tenant: str = TENANT) -> None:
        self._base_url = base_url
        self._headers = auth_headers(api_key)
        self._tenant = tenant
        self._http = http_client(base_url, timeout=120.0, headers=self._headers)
        self._terminals: dict[str, _TerminalConnection] = {}
        self._keys_honoured: bool | None = None
        self.retry_for = 60.0  # seconds a mutating call keeps retrying

    def _honours_idempotency_keys(self) -> bool:
        """A cluster control plane replays retries; a single daemon doesn't."""
        if self._keys_honoured is None:
            try:
                self._keys_honoured = self._http.get("/health", timeout=5.0).json().get("role") == "control-plane"
            except (httpx.HTTPError, ValueError):
                return False
        return self._keys_honoured

    def _mutate(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Send a request that changes something, with an Idempotency-Key.

        A request that never reached the server is always retried. Against a
        cluster, one that may have (a dropped connection, a 502/503/504, the VM
        busy with another operation) is retried too, with the same key, so it
        takes effect once.
        """
        headers = {"Idempotency-Key": uuid.uuid4().hex, **kwargs.pop("headers", {})}
        deadline = time.monotonic() + self.retry_for
        delay = 0.25
        while True:
            try:
                resp = self._http.request(method, path, headers=headers, **kwargs)
            except httpx.TransportError as e:
                sent = not isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout))
                if time.monotonic() >= deadline or (sent and not self._honours_idempotency_keys()):
                    raise
            else:
                if not self._worth_retrying(resp) or time.monotonic() >= deadline:
                    return resp
            time.sleep(delay)
            delay = min(delay * 2, 4.0)

    def _worth_retrying(self, resp: httpx.Response) -> bool:
        if resp.status_code not in _RETRY_STATUS and resp.status_code != 409:
            return False
        try:
            detail = str(resp.json().get("detail", ""))
        except ValueError:
            detail = resp.text
        if resp.status_code == 409 and not any(d in detail for d in _RETRY_DETAIL):
            return False
        if "capacity_exhausted" in detail:
            return False  # retrying won't make room
        return self._honours_idempotency_keys()

    def close(self) -> None:
        for conn in self._terminals.values():
            conn.close()
        self._terminals.clear()
        self._http.close()

    def create(self, *, template_id: str = "default", allow_internet_access: bool = True, use_pool: bool = True, use_pyroute2: bool = True, network_policy: dict | None = None, tenant: str | None = None, availability: str | None = None) -> dict:
        body = {}
        if network_policy is not None:
            body["network_policy"] = network_policy
        if availability is not None:
            body["availability"] = availability
        params = {"template_id": template_id, "allow_internet_access": allow_internet_access, "use_pool": use_pool, "use_pyroute2": use_pyroute2}
        if tenant or self._tenant:
            params["tenant"] = tenant or self._tenant
        resp = self._mutate("POST", "/vms", params=params, json=body or None)
        _raise_for_status(resp)
        return resp.json()["vm"]

    def kill(self, vm_id: str) -> None:
        self.disconnect_terminal(vm_id)
        resp = self._mutate("DELETE", f"/vms/{vm_id}")
        _raise_for_status(resp)

    def list(self) -> list[dict]:
        resp = self._http.get("/vms")
        _raise_for_status(resp)
        return resp.json()["vms"]

    def get(self, vm_id: str) -> dict | None:
        resp = self._http.get(f"/vms/{vm_id}")
        if resp.status_code == 404:
            return None
        _raise_for_status(resp)
        return resp.json()["vm"]

    def connect_terminal(self, vm_id: str, on_output: Callable[[bytes], None]) -> None:
        """Open WebSocket to VM terminal. Calls on_output with binary data from VM."""
        if vm_id in self._terminals:
            self.disconnect_terminal(vm_id)
        self._terminals[vm_id] = _TerminalConnection(self._base_url, f"/vms/{vm_id}/terminal", on_output, self._headers)

    def send_input(self, vm_id: str, data: bytes) -> None:
        conn = self._terminals.get(vm_id)
        if conn:
            conn.send(data)

    def disconnect_terminal(self, vm_id: str) -> None:
        conn = self._terminals.pop(vm_id, None)
        if conn:
            conn.close()

    # ── Command execution ──────────────────────────────────────────────────

    def exec_command(self, vm_id: str, command: str | list[str], timeout: float = 60) -> dict:
        """Execute a command in the VM via the guest agent. Returns {stdout, stderr, exit_code}."""
        if isinstance(command, str):
            cmd = ["/bin/sh", "-c", command]
        else:
            cmd = command
        resp = self._http.post(
            f"/vms/{vm_id}/exec",
            json={"cmd": cmd, "timeout": int(timeout)},
            timeout=timeout + 10,
        )
        _raise_for_status(resp)
        return resp.json()

    def run_code(self, vm_id: str, code: str, runtime: str | None = None, timeout: float = 60) -> dict:
        """Execute code in the VM. Returns {stdout, stderr, exit_code}."""
        if runtime is None:
            runtime = "python3"
        cmd = [runtime, "-c", code]
        return self.exec_command(vm_id, cmd, timeout=timeout)

    # ── Filesystem operations ──────────────────────────────────────────────

    def read_file(self, vm_id: str, path: str) -> bytes:
        """Read a file from the VM."""
        resp = self._http.get(f"/vms/{vm_id}/files", params={"path": path})
        _raise_for_status(resp)
        return resp.content

    def write_file(self, vm_id: str, path: str, content: bytes, mode: str = "0644") -> None:
        """Write a file to the VM."""
        resp = self._http.post(
            f"/vms/{vm_id}/files",
            params={"path": path, "mode": mode},
            content=content,
        )
        _raise_for_status(resp)

    def list_files(self, vm_id: str, path: str) -> list[dict]:
        """List files in a directory on the VM."""
        resp = self._http.get(f"/vms/{vm_id}/files/list", params={"path": path})
        _raise_for_status(resp)
        return resp.json()["entries"]

    # ── Network policy ─────────────────────────────────────────────────

    def update_network_policy(self, vm_id: str, policy: dict) -> None:
        """Update the network policy (credential injection rules) for a sandbox."""
        resp = self._http.put(f"/vms/{vm_id}/network-policy", json=policy)
        _raise_for_status(resp)

    def get_network_policy(self, vm_id: str) -> dict | None:
        """The sandbox's network policy, with credential values redacted."""
        resp = self._http.get(f"/vms/{vm_id}/network-policy")
        _raise_for_status(resp)
        return resp.json().get("network_policy")

    # ── Sandbox lifecycle ────────────────────────────────────────────────

    def pause(self, vm_id: str) -> None:
        self.disconnect_terminal(vm_id)
        resp = self._mutate("POST", f"/vms/{vm_id}/pause")
        _raise_for_status(resp)

    def fork(self, vm_id: str, count: int = 1) -> list[dict]:
        """Fork a running VM into ``count`` new VMs (cluster control plane only)."""
        resp = self._mutate("POST", f"/vms/{vm_id}/fork", json={"count": count}, timeout=300.0)
        if resp.status_code >= 400:
            raise RuntimeError(f"Fork failed ({resp.status_code}): {resp.text}")
        return resp.json()["vms"]

    def resume(self, vm_id: str) -> dict:
        resp = self._mutate("POST", f"/vms/{vm_id}/resume")
        if resp.status_code >= 400:
            detail = resp.text
            try:
                detail = resp.json().get("detail", resp.text)
            except Exception:
                pass
            raise RuntimeError(f"Resume failed ({resp.status_code}): {detail}")
        return resp.json()["vm"]

    def set_timeout(self, vm_id: str, timeout_seconds: float, policy: str = "kill") -> None:
        resp = self._http.patch(
            f"/vms/{vm_id}",
            json={"timeout_seconds": timeout_seconds, "timeout_policy": policy},
        )
        _raise_for_status(resp)

    # ── Template methods ───────────────────────────────────────────────────

    def build_template(self, name: str, dockerfile: str, rootfs_size_mb: int = 500) -> dict:
        resp = self._http.post(
            "/templates/build",
            json={"name": name, "dockerfile": dockerfile, "rootfs_size_mb": rootfs_size_mb},
            timeout=600.0,
        )
        _raise_for_status(resp)
        return resp.json()

    def list_templates(self) -> list[dict]:
        resp = self._http.get("/templates")
        _raise_for_status(resp)
        return resp.json()["templates"]

    def get_template(self, template_id: str) -> dict | None:
        resp = self._http.get(f"/templates/{template_id}")
        if resp.status_code == 404:
            return None
        _raise_for_status(resp)
        return resp.json()["template"]

    def delete_template(self, template_id: str) -> None:
        resp = self._http.delete(f"/templates/{template_id}")
        _raise_for_status(resp)

    # ── Volumes ───────────────────────────────────────────────────────────

    def list_volumes(self) -> list[dict]:
        resp = self._http.get("/volumes")
        _raise_for_status(resp)
        return resp.json()["volumes"]

    def create_volume(self, name: str, size_gib: int | None = None) -> dict:
        body: dict = {"name": name}
        if size_gib is not None:
            body["size_gib"] = size_gib
        resp = self._http.post("/volumes", json=body)
        _raise_for_status(resp)
        return resp.json()["volume"]

    def snapshot_volume(self, volume: str, name: str) -> dict:
        resp = self._http.post(f"/volumes/{volume}/snapshot", json={"name": name})
        _raise_for_status(resp)
        return resp.json()["volume"]

    def fork_volume(self, volume: str, name: str) -> dict:
        resp = self._http.post(f"/volumes/{volume}/fork", json={"name": name})
        _raise_for_status(resp)
        return resp.json()["volume"]

    def delete_volume(self, volume: str) -> None:
        """Delete a volume by ID or name."""
        resp = self._http.delete(f"/volumes/{volume}")
        _raise_for_status(resp)

    def list_mounts(self, vm_id: str) -> list[dict]:
        resp = self._http.get(f"/vms/{vm_id}/mounts")
        _raise_for_status(resp)
        return resp.json()["mounts"]

    def mount(self, vm_id: str, volume: str, path: str, *, subpath: str = "", readonly: bool = False,
              exclusive: bool = False) -> dict:
        resp = self._http.post(
            f"/vms/{vm_id}/mounts",
            json={"volume": volume, "path": path, "subpath": subpath, "readonly": readonly, "exclusive": exclusive},
        )
        _raise_for_status(resp)
        return resp.json()["mount"]

    def unmount(self, vm_id: str, path: str) -> None:
        resp = self._http.delete(f"/vms/{vm_id}/mounts", params={"path": path})
        _raise_for_status(resp)

    # ── Config / limits ────────────────────────────────────────────────────

    def get_config(self) -> dict:
        resp = self._http.get("/config")
        _raise_for_status(resp)
        return resp.json()

    def patch_config(self, overrides: dict) -> dict:
        resp = self._http.patch("/config", json=overrides)
        _raise_for_status(resp)
        return resp.json()

    # ── Metrics ───────────────────────────────────────────────────────────

    def get_metrics(self, vm_id: str, window: int = 60) -> list[dict]:
        resp = self._http.get(f"/vms/{vm_id}/metrics", params={"window": window})
        _raise_for_status(resp)
        return resp.json()["samples"]

    @staticmethod
    def is_daemon_running(base_url: str = DEFAULT_URL) -> bool:
        try:
            with http_client(base_url, timeout=1.0) as c:
                return c.get("/health").status_code == 200
        except Exception:
            return False

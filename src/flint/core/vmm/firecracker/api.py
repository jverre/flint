"""HTTP-over-UDS client for the Firecracker API socket."""

from __future__ import annotations

import json
import socket
import time

from ...config import log
from ..._jailfs import connect_unix
from ..common import log_tail


def request(sock_path: str, method: str, path: str, body: dict) -> str:
    """One request; returns the raw response (status line, headers and body)."""
    payload = json.dumps(body).encode()
    request = (
        f"{method} {path} HTTP/1.1\r\n"
        f"Host: localhost\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(payload)}\r\n"
        f"Accept: application/json\r\n"
        f"\r\n"
    ).encode() + payload
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connect_unix(sock, sock_path)
        sock.sendall(request)
        response = sock.recv(4096).decode(errors="replace")
        status_line = response.split("\r\n", 1)[0] if response else "(empty)"
        if not status_line.startswith("HTTP/1.1 2"):
            log.error("FC %s %s → %s\n%s", method, path, status_line, response)
        else:
            log.debug("FC %s %s → %s", method, path, status_line)
        return response
    finally:
        sock.close()


def put(sock_path: str, path: str, body: dict) -> str:
    return request(sock_path, "PUT", path, body)


def patch(sock_path: str, path: str, body: dict) -> str:
    return request(sock_path, "PATCH", path, body)


def status_ok(resp: str) -> bool:
    status_line = resp.split("\r\n", 1)[0] if resp else ""
    return status_line.startswith("HTTP/1.1 2")


def response_body(resp: str) -> str:
    return resp.split("\r\n\r\n", 1)[-1]


def set_vm_state(sock_path: str, state: str) -> str:
    """Pause or resume the guest (``state`` is ``"Paused"`` or ``"Resumed"``)."""
    return patch(sock_path, "/vm", {"state": state})


def load_snapshot(sock_path: str, snapshot_path: str, mem_path: str, network_overrides: list[dict] | None = None) -> None:
    """Load a snapshot (paths relative to the jail) without resuming it.

    Dirty-page tracking lets a later pause write only the pages touched since.
    """
    body: dict = {
        "snapshot_path": snapshot_path,
        "mem_backend": {"backend_type": "File", "backend_path": mem_path},
        "track_dirty_pages": True,
        "resume_vm": False,
    }
    if network_overrides:
        body["network_overrides"] = network_overrides
    resp = put(sock_path, "/snapshot/load", body)
    if not status_ok(resp):
        raise RuntimeError(f"snapshot/load failed: {resp}")


def wait_for_api_socket(socket_path: str, timeout: float = 5.0, *, process=None, log_path: str = "", cgroup: str = "") -> None:
    """Block until the API socket accepts connections.

    Fails fast if ``process`` exits first, and then (or on timeout) includes
    the tail of ``log_path``. With ``cgroup``, also checks the VMM joined it.
    """
    def failure(message: str) -> str:
        tail = log_tail(log_path, 4096)
        return f"{message}\n{tail}" if tail else message

    t0 = time.monotonic()
    while True:
        if process is not None and process.poll() is not None:
            raise RuntimeError(failure(f"Firecracker/jailer exited with code {process.returncode}"))
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connect_unix(s, socket_path)
            if cgroup:
                from ..._limits import verify_process
                verify_process(cgroup, process.pid)
            return
        except (ConnectionRefusedError, FileNotFoundError, OSError):
            if time.monotonic() - t0 > timeout:
                raise TimeoutError(failure(f"Firecracker API socket not ready after {timeout}s"))
            time.sleep(0.001)
        finally:
            s.close()

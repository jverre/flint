"""HTTP-over-UDS client for the cloud-hypervisor API.

Cloud-Hypervisor exposes a REST API at ``--api-socket`` implementing
``/api/v1/vm.{create,boot,pause,resume,snapshot,restore,delete,info}`` and
``/api/v1/vmm.{ping,shutdown}``. This module wraps that protocol in the same
raw-HTTP-over-UDS style used by :mod:`_firecracker` so both backends can run in
a jailer-style environment where only the control socket is reachable.
"""

from __future__ import annotations

import json
import socket
import time

from .config import log


def _request(sock_path: str, method: str, path: str, body: dict | None = None) -> tuple[int, bytes]:
    payload = json.dumps(body).encode() if body is not None else b""
    headers = [
        f"{method} {path} HTTP/1.1",
        "Host: localhost",
        "Accept: application/json",
    ]
    if payload:
        headers.append("Content-Type: application/json")
        headers.append(f"Content-Length: {len(payload)}")
    request = ("\r\n".join(headers) + "\r\n\r\n").encode() + payload

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(sock_path)
        sock.sendall(request)
        chunks: list[bytes] = []
        sock.settimeout(5.0)
        while True:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            chunks.append(chunk)
            # cloud-hypervisor closes the connection after the response; we stop
            # once we have a Content-Length worth or the peer closes.
            if len(b"".join(chunks)) > 65536:
                break
    finally:
        sock.close()

    raw = b"".join(chunks)
    if not raw:
        return 0, b""
    head, _, body_bytes = raw.partition(b"\r\n\r\n")
    status_line = head.split(b"\r\n", 1)[0].decode(errors="replace")
    try:
        status_code = int(status_line.split(" ", 2)[1])
    except (IndexError, ValueError):
        status_code = 0
    if status_code >= 400:
        log.error("CH %s %s → %s %s", method, path, status_code, body_bytes[:200])
    else:
        log.debug("CH %s %s → %s", method, path, status_code)
    return status_code, body_bytes


def ch_put(sock_path: str, path: str, body: dict | None = None) -> tuple[int, bytes]:
    return _request(sock_path, "PUT", path, body)


def ch_post(sock_path: str, path: str, body: dict | None = None) -> tuple[int, bytes]:
    return _request(sock_path, "POST", path, body)


def ch_get(sock_path: str, path: str) -> tuple[int, bytes]:
    return _request(sock_path, "GET", path, None)


def ch_status_ok(status: int) -> bool:
    return 200 <= status < 300


def wait_for_api_socket(socket_path: str, timeout: float = 10.0) -> None:
    t0 = time.monotonic()
    while True:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(socket_path)
            return
        except (ConnectionRefusedError, FileNotFoundError, OSError):
            if time.monotonic() - t0 > timeout:
                raise TimeoutError("cloud-hypervisor API socket not ready")
            time.sleep(0.005)
        finally:
            s.close()


def vm_create(sock_path: str, config: dict) -> None:
    status, body = ch_put(sock_path, "/api/v1/vm.create", config)
    if not ch_status_ok(status):
        raise RuntimeError(f"vm.create failed: {status} {body!r}")


def vm_boot(sock_path: str) -> None:
    status, body = ch_put(sock_path, "/api/v1/vm.boot")
    if not ch_status_ok(status):
        raise RuntimeError(f"vm.boot failed: {status} {body!r}")


def vm_pause(sock_path: str) -> None:
    status, body = ch_put(sock_path, "/api/v1/vm.pause")
    if not ch_status_ok(status):
        raise RuntimeError(f"vm.pause failed: {status} {body!r}")


def vm_resume(sock_path: str) -> None:
    status, body = ch_put(sock_path, "/api/v1/vm.resume")
    if not ch_status_ok(status):
        raise RuntimeError(f"vm.resume failed: {status} {body!r}")


def vm_snapshot(sock_path: str, destination_url: str) -> None:
    status, body = ch_put(
        sock_path, "/api/v1/vm.snapshot", {"destination_url": destination_url}
    )
    if not ch_status_ok(status):
        raise RuntimeError(f"vm.snapshot failed: {status} {body!r}")


def vm_restore(sock_path: str, source_url: str) -> None:
    status, body = ch_put(
        sock_path, "/api/v1/vm.restore", {"source_url": source_url}
    )
    if not ch_status_ok(status):
        raise RuntimeError(f"vm.restore failed: {status} {body!r}")


def vmm_shutdown(sock_path: str) -> None:
    try:
        ch_put(sock_path, "/api/v1/vmm.shutdown")
    except Exception:
        pass

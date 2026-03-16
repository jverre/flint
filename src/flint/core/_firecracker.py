import json
import socket
import time
import urllib.request

from .config import log, GUEST_IP, AGENT_PORT
from ._netns import _enter_netns, _restore_netns


def _fc_request(sock_path: str, method: str, path: str, body: dict) -> str:
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
        sock.connect(sock_path)
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


def _fc_put(sock_path: str, path: str, body: dict) -> str:
    return _fc_request(sock_path, "PUT", path, body)


def _fc_patch(sock_path: str, path: str, body: dict) -> str:
    return _fc_request(sock_path, "PATCH", path, body)


def _fc_status_ok(resp: str) -> bool:
    status_line = resp.split("\r\n", 1)[0] if resp else ""
    return status_line.startswith("HTTP/1.1 2")


def _wait_for_api_socket(socket_path: str, timeout: float = 5.0) -> None:
    t0 = time.monotonic()
    while True:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(socket_path)
            return
        except (ConnectionRefusedError, FileNotFoundError, OSError):
            if time.monotonic() - t0 > timeout:
                raise TimeoutError("Firecracker API socket not ready")
            time.sleep(0.001)
        finally:
            s.close()


def _wait_for_agent(ns_name: str, retries: int = 500) -> str:
    """Wait for flintd guest agent to become healthy. Returns agent_url."""
    agent_url = f"http://{GUEST_IP}:{AGENT_PORT}"
    health_url = f"{agent_url}/health"
    orig_fd = _enter_netns(ns_name)
    try:
        for _ in range(retries):
            try:
                req = urllib.request.Request(health_url, method="GET")
                with urllib.request.urlopen(req, timeout=0.1) as resp:
                    if resp.status == 200:
                        return agent_url
            except Exception:
                time.sleep(0.001)
        raise TimeoutError(f"Agent health check failed after {retries} attempts")
    finally:
        _restore_netns(orig_fd)


def _tcp_connect(ns_name: str, retries: int = 500) -> socket.socket:
    """Legacy: Connect to guest TCP port inside the namespace. Raises on failure."""
    orig_fd = _enter_netns(ns_name)
    try:
        for _ in range(retries):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            try:
                sock.settimeout(0.05)
                sock.connect((GUEST_IP, AGENT_PORT))
                sock.settimeout(None)
                return sock
            except (ConnectionRefusedError, TimeoutError, OSError):
                sock.close()
                time.sleep(0.001)
        raise TimeoutError(f"TCP connect failed after {retries} attempts")
    finally:
        _restore_netns(orig_fd)

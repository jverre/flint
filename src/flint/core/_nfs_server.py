"""Flint's host NFS server (``services/r2nfs``).

One process per host serves volume mounts and, with the R2 storage backend,
R2 workspaces. It outlives the daemon, like the VMs whose mounts it serves:
a restarted daemon adopts the running server instead of breaking those
mounts, and replays its registrations, which is idempotent.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config
from .config import log

BINARY = os.environ.get(
    "FLINT_R2NFS_BINARY",
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "services", "r2nfs", "r2nfs"),
)
VOLUMES_DIR = os.path.join(config.DAEMON_DIR, "volumes")
_PID_PATH = os.path.join(config.DAEMON_DIR, "r2nfs.pid")
_LOG_PATH = os.path.join(config.DAEMON_DIR, "r2nfs.log")


class NfsServerError(RuntimeError):
    pass


def _mgmt(method: str, path: str, body: dict | None = None, timeout: float = 5) -> dict:
    url = f"http://127.0.0.1:{config.R2NFS_MGMT_PORT}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        try:
            detail = json.loads(detail).get("error", detail)
        except (ValueError, AttributeError):
            pass
        raise NfsServerError(detail.strip()) from None
    except OSError as e:
        raise NfsServerError(f"NFS server unreachable: {e}") from None


def health() -> dict | None:
    try:
        return _mgmt("GET", "/health", timeout=2)
    except NfsServerError:
        return None


def ensure_running() -> None:
    """Adopt the running server if it serves what this daemon needs, else (re)start it."""
    want_r2 = config.STORAGE_BACKEND == "r2"
    current = health()
    if current and current.get("volumes_dir") == VOLUMES_DIR and current.get("r2") == want_r2:
        return
    if current:
        log.info("Replacing NFS server (volumes_dir=%s r2=%s; want %s r2=%s)",
                 current.get("volumes_dir"), current.get("r2"), VOLUMES_DIR, want_r2)
        _stop_recorded()
        if health():
            raise NfsServerError(
                f"Another NFS server is on 127.0.0.1:{config.R2NFS_MGMT_PORT} serving "
                f"{current.get('volumes_dir')!r}; stop it or set FLINT_R2NFS_MGMT_PORT/FLINT_R2NFS_PORT"
            )

    binary = os.path.realpath(BINARY)
    if not os.path.isfile(binary):
        raise NfsServerError(
            f"NFS server binary not found at {binary}. "
            "Build it with: cd services/r2nfs && go build -o r2nfs ."
        )
    os.makedirs(VOLUMES_DIR, exist_ok=True)
    cmd = [
        binary,
        f"--listen={config.BRIDGE_IP}:{config.R2NFS_PORT}",
        f"--mgmt=127.0.0.1:{config.R2NFS_MGMT_PORT}",
        f"--volumes-dir={VOLUMES_DIR}",
    ]
    env = os.environ.copy()
    if want_r2:
        os.makedirs(config.R2_CACHE_DIR, exist_ok=True)
        cmd += [
            f"--bucket={config.R2_BUCKET}",
            f"--cache-dir={config.R2_CACHE_DIR}",
            f"--cache-size-mb={config.R2_CACHE_SIZE_MB}",
        ]
        # Credentials via the environment so they don't show in `ps`.
        env["R2_ACCOUNT_ID"] = config.R2_ACCOUNT_ID
        env["R2_ACCESS_KEY_ID"] = config.R2_ACCESS_KEY_ID
        env["R2_SECRET_ACCESS_KEY"] = config.R2_SECRET_ACCESS_KEY

    log.info("Starting NFS server: %s", " ".join(cmd))
    with open(_LOG_PATH, "ab") as log_fd:
        proc = subprocess.Popen(
            cmd, env=env, stdout=log_fd, stderr=subprocess.STDOUT, start_new_session=True,
        )
    with open(_PID_PATH, "w") as f:
        f.write(str(proc.pid))

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if health():
            log.info("NFS server ready (pid=%d)", proc.pid)
            return
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    raise NfsServerError(f"NFS server failed to start; see {_LOG_PATH}")


def _stop_recorded() -> None:
    try:
        with open(_PID_PATH) as f:
            pid = int(f.read().strip())
        os.kill(pid, signal.SIGTERM)
    except (OSError, ValueError):
        return
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and health():
        time.sleep(0.1)


# ── Volume mounts ────────────────────────────────────────────────────────

def register_mount(mount_id: str, client_ip: str, volume_id: str, subpath: str, readonly: bool) -> None:
    _mgmt("POST", "/mounts", {
        "id": mount_id, "client_ip": client_ip, "volume": volume_id,
        "subpath": subpath, "readonly": readonly,
    })


def deregister_mount(mount_id: str) -> None:
    _mgmt("DELETE", f"/mounts/{urllib.parse.quote(mount_id)}")


# ── R2 workspaces ────────────────────────────────────────────────────────

def register_workspace(client_ip: str, vm_id: str, template_id: str) -> None:
    _mgmt("POST", "/exports", {"client_ip": client_ip, "vm_id": vm_id, "template_id": template_id})


def deregister_workspace(client_ip: str) -> None:
    _mgmt("DELETE", f"/exports/{urllib.parse.quote(client_ip)}")

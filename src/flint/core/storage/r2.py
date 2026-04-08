"""Cloudflare R2 storage backend.

Runs the ``r2nfs`` Go service on the host, which serves R2 objects
as an NFS filesystem with overlay semantics (read-only template base
layer + per-sandbox writable layer).  Guest VMs mount via NFS over
the bridge network.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import urllib.parse
import urllib.request

from ..config import (
    log,
    BRIDGE_IP,
    WORKSPACE_DIR,
    R2_ACCOUNT_ID,
    R2_ACCESS_KEY_ID,
    R2_SECRET_ACCESS_KEY,
    R2_BUCKET,
    R2_CACHE_DIR,
    R2_CACHE_SIZE_MB,
    R2NFS_PORT,
    R2NFS_MGMT_PORT,
)
from .base import StorageBackend

_R2NFS_BINARY = os.environ.get(
    "FLINT_R2NFS_BINARY",
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "services", "r2nfs", "r2nfs"),
)

_MGMT_BASE = f"http://127.0.0.1:{R2NFS_MGMT_PORT}"


class R2StorageBackend(StorageBackend):
    kind = "r2"

    def __init__(self) -> None:
        self._process: subprocess.Popen | None = None

    # ── Lifecycle ──────────────────────────────────────────────────────

    def start(self) -> None:
        self._validate_config()
        self._spawn_r2nfs()

    def stop(self) -> None:
        if self._process is None:
            return
        log.info("Stopping r2nfs (pid=%d)", self._process.pid)
        self._process.send_signal(signal.SIGTERM)
        try:
            self._process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._process.kill()
        self._process = None

    def is_running(self) -> bool:
        return self._health_check()

    # ── Per-sandbox setup / teardown ──────────────────────────────────

    def setup_sandbox(
        self, vm_id: str, template_id: str, veth_ip: str, ns_name: str, agent_url: str,
    ) -> None:
        self._register_export(veth_ip, vm_id, template_id)
        self._mount_nfs_in_guest(
            agent_url=agent_url,
            ns_name=ns_name,
            source=f"{BRIDGE_IP}:/{vm_id}",
            target=WORKSPACE_DIR,
            options="vers=3,soft,timeo=50,retrans=3,nolock",
        )
        log.info("Mounted R2 in sandbox %s at %s", vm_id[:8], WORKSPACE_DIR)

    def teardown_sandbox(self, vm_id: str, veth_ip: str) -> None:
        if veth_ip:
            self._deregister_export(veth_ip)

    # ── Internals ─────────────────────────────────────────────────────

    def _validate_config(self) -> None:
        if not R2_ACCOUNT_ID or not R2_ACCESS_KEY_ID or not R2_SECRET_ACCESS_KEY:
            raise ValueError(
                "R2 storage backend requires FLINT_R2_ACCOUNT_ID, "
                "FLINT_R2_ACCESS_KEY_ID, and FLINT_R2_SECRET_ACCESS_KEY"
            )
        binary = os.path.realpath(_R2NFS_BINARY)
        if not os.path.isfile(binary):
            raise FileNotFoundError(
                f"r2nfs binary not found at {binary}. "
                "Build it with: cd services/r2nfs && go build -o r2nfs ."
            )

    def _spawn_r2nfs(self) -> None:
        os.makedirs(R2_CACHE_DIR, exist_ok=True)
        binary = os.path.realpath(_R2NFS_BINARY)

        cmd = [
            binary,
            f"--listen={BRIDGE_IP}:{R2NFS_PORT}",
            f"--mgmt=127.0.0.1:{R2NFS_MGMT_PORT}",
            f"--bucket={R2_BUCKET}",
            f"--cache-dir={R2_CACHE_DIR}",
            f"--cache-size-mb={R2_CACHE_SIZE_MB}",
        ]

        # Pass credentials via environment to avoid leaking them in `ps` output.
        env = os.environ.copy()
        env["R2_ACCOUNT_ID"] = R2_ACCOUNT_ID
        env["R2_ACCESS_KEY_ID"] = R2_ACCESS_KEY_ID
        env["R2_SECRET_ACCESS_KEY"] = R2_SECRET_ACCESS_KEY

        log.info("Starting r2nfs: %s", " ".join(cmd))
        self._process = subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        for _ in range(20):
            if self._health_check():
                log.info("r2nfs is ready (pid=%d)", self._process.pid)
                return
            time.sleep(0.5)

        raise RuntimeError("r2nfs failed to start within 10 seconds")

    def _health_check(self) -> bool:
        try:
            with urllib.request.urlopen(f"{_MGMT_BASE}/health", timeout=2) as resp:
                return resp.status == 200
        except Exception:
            return False

    def _register_export(self, client_ip: str, vm_id: str, template_id: str) -> None:
        data = json.dumps({
            "client_ip": client_ip,
            "vm_id": vm_id,
            "template_id": template_id,
        }).encode()
        req = urllib.request.Request(f"{_MGMT_BASE}/exports", data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Failed to register R2 export: {resp.read()}")
        log.info("Registered R2 export: sandbox=%s ip=%s", vm_id[:8], client_ip)

    def _deregister_export(self, client_ip: str) -> None:
        try:
            url = f"{_MGMT_BASE}/exports/{urllib.parse.quote(client_ip)}"
            req = urllib.request.Request(url, method="DELETE")
            with urllib.request.urlopen(req, timeout=5):
                pass
        except Exception as e:
            log.warning("Failed to deregister R2 export for %s: %s", client_ip, e)

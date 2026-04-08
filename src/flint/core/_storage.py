"""Storage backend manager for sandbox filesystems.

Supports three backends:
  - local:    Files on the guest VM's ext4 rootfs (default, no changes needed)
  - s3_files: AWS S3 Files NFS mount inside the guest VM
  - r2:       Cloudflare R2 via the r2nfs bridge service
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
import urllib.parse
import urllib.request

from .config import (
    log,
    STORAGE_BACKEND,
    WORKSPACE_DIR,
    S3_FILES_NFS_ENDPOINT,
    R2_ACCOUNT_ID,
    R2_ACCESS_KEY_ID,
    R2_SECRET_ACCESS_KEY,
    R2_BUCKET,
    R2_CACHE_DIR,
    R2_CACHE_SIZE_MB,
    R2NFS_PORT,
    R2NFS_MGMT_PORT,
    BRIDGE_IP,
)

_R2NFS_BINARY = os.environ.get(
    "FLINT_R2NFS_BINARY",
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "services", "r2nfs", "r2nfs"),
)


class StorageManager:
    """Manages the storage backend lifecycle for sandbox filesystems."""

    def __init__(self, backend: str | None = None) -> None:
        self.backend = backend or STORAGE_BACKEND
        self._r2nfs_process: subprocess.Popen | None = None

        if self.backend not in ("local", "s3_files", "r2"):
            raise ValueError(f"Unknown storage backend: {self.backend}")

    @property
    def is_cloud(self) -> bool:
        return self.backend in ("s3_files", "r2")

    def start(self) -> None:
        """Start backend services. Called during daemon init."""
        if self.backend == "r2":
            self._start_r2nfs()
        elif self.backend == "s3_files":
            self._validate_s3_files_config()
        log.info("Storage backend started: %s", self.backend)

    def stop(self) -> None:
        """Stop backend services. Called during daemon shutdown."""
        if self._r2nfs_process is not None:
            log.info("Stopping r2nfs service")
            self._r2nfs_process.send_signal(signal.SIGTERM)
            try:
                self._r2nfs_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._r2nfs_process.kill()
            self._r2nfs_process = None

    def is_running(self) -> bool:
        """Check if the backend service is healthy."""
        if self.backend == "local":
            return True
        if self.backend == "r2":
            return self._r2nfs_health_check()
        if self.backend == "s3_files":
            return True  # No service to check
        return False

    def setup_sandbox(
        self,
        vm_id: str,
        template_id: str,
        veth_ip: str,
        ns_name: str,
        agent_url: str,
    ) -> None:
        """Configure storage for a new sandbox.

        For cloud backends, this registers an NFS export and tells
        the guest agent to mount the workspace.
        """
        if self.backend == "local":
            return

        if self.backend == "r2":
            self._register_r2_export(veth_ip, vm_id, template_id)
            nfs_source = f"{BRIDGE_IP}:/{vm_id}"
            nfs_options = "vers=3,soft,timeo=50,retrans=3,nolock"
        elif self.backend == "s3_files":
            nfs_source = S3_FILES_NFS_ENDPOINT
            nfs_options = "nfsvers=4.1"
        else:
            return

        self._mount_nfs_in_guest(agent_url, ns_name, nfs_source, WORKSPACE_DIR, nfs_options)

    def teardown_sandbox(self, vm_id: str, veth_ip: str) -> None:
        """Clean up storage for a destroyed sandbox."""
        if self.backend == "r2" and veth_ip:
            self._deregister_r2_export(veth_ip)

    # ── R2 backend helpers ──────────────────────────────────────────────

    def _start_r2nfs(self) -> None:
        """Start the r2nfs NFS bridge service."""
        binary = os.path.realpath(_R2NFS_BINARY)
        if not os.path.isfile(binary):
            raise FileNotFoundError(
                f"r2nfs binary not found at {binary}. "
                "Build it with: cd services/r2nfs && go build -o r2nfs ."
            )

        if not R2_ACCOUNT_ID or not R2_ACCESS_KEY_ID or not R2_SECRET_ACCESS_KEY:
            raise ValueError(
                "R2 storage backend requires FLINT_R2_ACCOUNT_ID, "
                "FLINT_R2_ACCESS_KEY_ID, and FLINT_R2_SECRET_ACCESS_KEY"
            )

        os.makedirs(R2_CACHE_DIR, exist_ok=True)

        cmd = [
            binary,
            f"--listen={BRIDGE_IP}:{R2NFS_PORT}",
            f"--mgmt=127.0.0.1:{R2NFS_MGMT_PORT}",
            f"--bucket={R2_BUCKET}",
            f"--account-id={R2_ACCOUNT_ID}",
            f"--access-key={R2_ACCESS_KEY_ID}",
            f"--secret-key={R2_SECRET_ACCESS_KEY}",
            f"--cache-dir={R2_CACHE_DIR}",
            f"--cache-size-mb={R2_CACHE_SIZE_MB}",
        ]

        log.info("Starting r2nfs: %s", " ".join(cmd[:3]) + " ...")
        self._r2nfs_process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        # Wait for it to be ready.
        for _ in range(20):
            if self._r2nfs_health_check():
                log.info("r2nfs is ready (pid=%d)", self._r2nfs_process.pid)
                return
            time.sleep(0.5)

        raise RuntimeError("r2nfs failed to start within 10 seconds")

    def _r2nfs_health_check(self) -> bool:
        """Check if the r2nfs management API is responding."""
        try:
            url = f"http://127.0.0.1:{R2NFS_MGMT_PORT}/health"
            with urllib.request.urlopen(url, timeout=2) as resp:
                return resp.status == 200
        except Exception:
            return False

    def _register_r2_export(self, client_ip: str, vm_id: str, template_id: str) -> None:
        """Register an NFS export for a sandbox in r2nfs."""
        url = f"http://127.0.0.1:{R2NFS_MGMT_PORT}/exports"
        data = json.dumps({
            "client_ip": client_ip,
            "vm_id": vm_id,
            "template_id": template_id,
        }).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Failed to register R2 export: {resp.read()}")
        log.info("Registered R2 export for sandbox %s (ip=%s)", vm_id[:8], client_ip)

    def _deregister_r2_export(self, client_ip: str) -> None:
        """Remove an NFS export for a sandbox from r2nfs."""
        try:
            url = f"http://127.0.0.1:{R2NFS_MGMT_PORT}/exports/{urllib.parse.quote(client_ip)}"
            req = urllib.request.Request(url, method="DELETE")
            with urllib.request.urlopen(req, timeout=5):
                pass
        except Exception as e:
            log.warning("Failed to deregister R2 export for %s: %s", client_ip, e)

    # ── S3 Files helpers ────────────────────────────────────────────────

    def _validate_s3_files_config(self) -> None:
        if not S3_FILES_NFS_ENDPOINT:
            raise ValueError(
                "S3 Files storage backend requires FLINT_S3_FILES_NFS_ENDPOINT "
                "(e.g., 'fs-abc123.s3-files.us-east-1.amazonaws.com:/')"
            )

    # ── Guest agent helpers ─────────────────────────────────────────────

    def _mount_nfs_in_guest(
        self,
        agent_url: str,
        ns_name: str,
        source: str,
        target: str,
        options: str,
    ) -> None:
        """Tell the guest agent to mount an NFS filesystem."""
        url = f"{agent_url}/mount/nfs"
        data = json.dumps({
            "source": source,
            "target": target,
            "options": options,
        }).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")

        # Enter the sandbox's network namespace to reach the guest agent.
        from .backends.linux_firecracker import _enter_netns, _restore_netns
        orig_fd = _enter_netns(ns_name)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status != 200:
                    body = resp.read()
                    log.error("NFS mount failed in guest: %s", body)
        finally:
            _restore_netns(orig_fd)

"""Abstract base class for sandbox storage backends."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod

from ..config import log


class StorageBackend(ABC):
    """Plugin interface for sandbox filesystem storage.

    Every storage backend must implement this interface. The daemon
    calls these methods at well-defined lifecycle points:

        daemon start  → start()
        POST /vms     → setup_sandbox(...)
        DELETE /vms   → teardown_sandbox(...)
        GET /health   → is_running()
        daemon stop   → stop()
    """

    kind: str  # "local", "s3_files", "r2"

    @property
    def is_cloud(self) -> bool:
        """True if this backend stores data remotely (not on the local rootfs)."""
        return self.kind != "local"

    @abstractmethod
    def start(self) -> None:
        """Start any backend services (e.g., NFS bridge process).

        Called once during daemon initialization. For backends that
        don't require a long-running service, this is a no-op or
        a config validation step.
        """

    @abstractmethod
    def stop(self) -> None:
        """Stop backend services. Called during daemon shutdown."""

    @abstractmethod
    def is_running(self) -> bool:
        """Return True if the backend is healthy and ready to serve."""

    @abstractmethod
    def setup_sandbox(
        self,
        vm_id: str,
        template_id: str,
        veth_ip: str,
        ns_name: str,
        agent_url: str,
    ) -> None:
        """Configure storage for a newly created sandbox.

        Called after the VM is running and the guest agent is healthy.
        For cloud backends this typically registers an NFS export and
        mounts it inside the guest.
        """

    @abstractmethod
    def teardown_sandbox(self, vm_id: str, veth_ip: str) -> None:
        """Clean up storage for a destroyed sandbox.

        Called before the VM is killed. For cloud backends this
        deregisters the NFS export so no stale state remains.
        """

    # ── Shared helpers for cloud backends ──────────────────────────────

    def _mount_nfs_in_guest(
        self,
        agent_url: str,
        ns_name: str,
        source: str,
        target: str,
        options: str,
    ) -> None:
        """Tell the guest agent to mount an NFS filesystem.

        Enters the sandbox's network namespace to reach the guest
        agent, then POSTs to ``/mount/nfs``.
        """
        url = f"{agent_url}/mount/nfs"
        data = json.dumps({
            "source": source,
            "target": target,
            "options": options,
        }).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")

        from ..backends.linux_firecracker import _enter_netns, _restore_netns
        orig_fd = _enter_netns(ns_name)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status != 200:
                    body = resp.read()
                    log.error("NFS mount failed in guest: %s", body)
        finally:
            _restore_netns(orig_fd)

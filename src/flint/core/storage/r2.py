"""Cloudflare R2 storage backend.

Workspaces are served by Flint's host NFS server (``r2nfs``, see
``flint.core._nfs_server``), which presents R2 objects with overlay
semantics (read-only template base layer + per-sandbox writable layer).
Guest VMs mount via NFS over the bridge network.
"""

from __future__ import annotations

from .. import _nfs_server
from ..config import (
    log,
    BRIDGE_IP,
    WORKSPACE_DIR,
    R2_ACCOUNT_ID,
    R2_ACCESS_KEY_ID,
    R2_SECRET_ACCESS_KEY,
)
from .base import StorageBackend


class R2StorageBackend(StorageBackend):
    kind = "r2"

    # ── Lifecycle ──────────────────────────────────────────────────────

    def start(self) -> None:
        if not R2_ACCOUNT_ID or not R2_ACCESS_KEY_ID or not R2_SECRET_ACCESS_KEY:
            raise ValueError(
                "R2 storage backend requires FLINT_R2_ACCOUNT_ID, "
                "FLINT_R2_ACCESS_KEY_ID, and FLINT_R2_SECRET_ACCESS_KEY"
            )
        _nfs_server.ensure_running()

    def stop(self) -> None:
        pass  # The NFS server outlives the daemon, like the VMs it serves.

    def is_running(self) -> bool:
        return bool((_nfs_server.health() or {}).get("r2"))

    # ── Per-sandbox setup / teardown ──────────────────────────────────

    def setup_sandbox(
        self, vm_id: str, template_id: str, veth_ip: str, ns_name: str, agent_url: str,
    ) -> None:
        _nfs_server.register_workspace(veth_ip, vm_id, template_id)
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
            try:
                _nfs_server.deregister_workspace(veth_ip)
            except _nfs_server.NfsServerError as e:
                log.warning("Failed to deregister R2 export for %s: %s", veth_ip, e)

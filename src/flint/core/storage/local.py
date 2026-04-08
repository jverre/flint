"""Local storage backend — files on the guest VM's ext4 rootfs.

This is the default backend. It requires no external services and
no changes to the existing sandbox lifecycle. All file I/O stays
on the local ext4 image managed by Firecracker's CoW pool.
"""

from __future__ import annotations

from .base import StorageBackend


class LocalStorageBackend(StorageBackend):
    kind = "local"

    def start(self) -> None:
        pass  # Nothing to start.

    def stop(self) -> None:
        pass  # Nothing to stop.

    def is_running(self) -> bool:
        return True  # Always available.

    def setup_sandbox(
        self, vm_id: str, template_id: str, veth_ip: str, ns_name: str, agent_url: str,
    ) -> None:
        pass  # No mount needed — workspace is on the rootfs.

    def teardown_sandbox(self, vm_id: str, veth_ip: str) -> None:
        pass  # No cleanup needed.

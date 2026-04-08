"""AWS S3 Files storage backend.

Mounts an S3 Files NFS endpoint inside guest VMs at the workspace
directory. S3 Files provides the NFS server — no host-side service
needed. See https://aws.amazon.com/s3/features/files/
"""

from __future__ import annotations

from ..config import log, S3_FILES_NFS_ENDPOINT, WORKSPACE_DIR
from .base import StorageBackend


class S3FilesStorageBackend(StorageBackend):
    kind = "s3_files"

    def start(self) -> None:
        if not S3_FILES_NFS_ENDPOINT:
            raise ValueError(
                "S3 Files storage backend requires FLINT_S3_FILES_NFS_ENDPOINT "
                "(e.g., 'fs-abc123.s3-files.us-east-1.amazonaws.com:/')"
            )
        log.info("S3 Files storage ready (endpoint: %s)", S3_FILES_NFS_ENDPOINT)

    def stop(self) -> None:
        pass  # No host-side service to stop.

    def is_running(self) -> bool:
        return True  # S3 Files is a managed AWS service.

    def setup_sandbox(
        self, vm_id: str, template_id: str, veth_ip: str, ns_name: str, agent_url: str,
    ) -> None:
        self._mount_nfs_in_guest(
            agent_url=agent_url,
            ns_name=ns_name,
            source=S3_FILES_NFS_ENDPOINT,
            target=WORKSPACE_DIR,
            options="nfsvers=4.1",
        )
        log.info("Mounted S3 Files in sandbox %s at %s", vm_id[:8], WORKSPACE_DIR)

    def teardown_sandbox(self, vm_id: str, veth_ip: str) -> None:
        pass  # NFS mount dies with the VM.

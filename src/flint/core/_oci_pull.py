"""OCI image pull and rootfs extraction via crane.

Pulls container images directly from any OCI-compatible registry
(Docker Hub, ghcr.io, etc.) and extracts them into ext4 rootfs
images — no Docker daemon required.
"""

from __future__ import annotations

import os
import shutil
import subprocess

from .config import log, CRANE_BINARY


def _ensure_crane() -> str:
    """Return path to crane binary, or raise if not found."""
    path = shutil.which(CRANE_BINARY)
    if path:
        return path
    raise FileNotFoundError(
        f"crane binary not found (looked for {CRANE_BINARY!r}). "
        "Install it with: bash scripts/install-crane.sh"
    )


def resolve_digest(image_ref: str) -> str:
    """Resolve an image reference to its immutable digest."""
    crane = _ensure_crane()
    result = subprocess.run(
        [crane, "digest", image_ref],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def pull_and_extract(
    image_ref: str,
    rootfs_path: str,
    *,
    size_mb: int = 500,
    inject_flint: bool = True,
    platform: str = "linux/amd64",
) -> str:
    """Pull an OCI image and create an ext4 rootfs from it.

    Uses ``crane export`` to flatten all image layers into a single
    filesystem tarball, then extracts into a freshly-created ext4 image.

    If *inject_flint* is True, copies the flintd binary and init-net.sh
    into the rootfs (needed for non-Flint images like ``ubuntu:22.04``).

    Returns the resolved image digest.
    """
    crane = _ensure_crane()

    # Resolve digest first (for cache keying)
    digest = resolve_digest(image_ref)
    log.info("Pulling %s (digest=%s)", image_ref, digest[:20])

    # Create empty ext4 image
    subprocess.run(["truncate", "-s", f"{size_mb}M", rootfs_path], check=True)
    subprocess.run(["mkfs.ext4", "-F", rootfs_path], check=True, capture_output=True)

    # Mount, export image layers, extract
    mount_dir = f"/tmp/flint-oci-mount-{os.getpid()}"
    os.makedirs(mount_dir, exist_ok=True)
    try:
        subprocess.run(["mount", rootfs_path, mount_dir], check=True)

        # crane export flattens all layers into a single tar stream
        crane_proc = subprocess.Popen(
            [crane, "export", "--platform", platform, image_ref, "-"],
            stdout=subprocess.PIPE,
        )
        subprocess.run(
            ["tar", "-x", "-C", mount_dir],
            stdin=crane_proc.stdout,
            check=True,
        )
        crane_proc.wait()
        if crane_proc.returncode != 0:
            raise RuntimeError(f"crane export failed with code {crane_proc.returncode}")

        if inject_flint:
            _inject_flint_into_rootfs(mount_dir)

    finally:
        subprocess.run(["umount", mount_dir], capture_output=True)
        shutil.rmtree(mount_dir, ignore_errors=True)

    log.info("Rootfs extracted to %s (%d MB)", rootfs_path, size_mb)
    return digest


def _inject_flint_into_rootfs(mount_point: str) -> None:
    """Copy flintd binary and init-net.sh into a mounted rootfs.

    This is needed when pulling non-Flint images (e.g., ubuntu:22.04)
    that don't already contain the guest agent.
    """
    from ._template_build import _build_flintd

    # Build or locate flintd binary
    flintd_dest = os.path.join(mount_point, "usr/local/bin/flintd")
    os.makedirs(os.path.dirname(flintd_dest), exist_ok=True)
    _build_flintd(flintd_dest)

    # Copy init-net.sh
    init_net_src = _find_init_net_sh()
    init_net_dest = os.path.join(mount_point, "etc/init-net.sh")
    shutil.copy2(init_net_src, init_net_dest)

    # Ensure executable
    os.chmod(flintd_dest, 0o755)
    os.chmod(init_net_dest, 0o755)

    # Ensure workspace directory
    os.makedirs(os.path.join(mount_point, "workspace"), exist_ok=True)

    log.info("Injected flintd + init-net.sh into rootfs")


def _find_init_net_sh() -> str:
    """Locate init-net.sh from assets directory."""
    project_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )
    candidate = os.path.join(project_root, "assets", "init-net.sh")
    if os.path.exists(candidate):
        return candidate
    raise FileNotFoundError(
        "init-net.sh not found in assets/. Place it at assets/init-net.sh."
    )

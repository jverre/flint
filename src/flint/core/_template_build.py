"""Template build pipeline: OCI image pull, rootfs extraction, snapshot creation."""

from __future__ import annotations

import os
import re
import shutil
import subprocess

from .config import log, TEMPLATES_DIR
from ._snapshot import create_golden_snapshot
from ._template_registry import register_template_artifact, update_template_artifact_status
from . import _oci_pull
from . import _oci_cache


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "template"


def _build_flintd(output_path: str) -> None:
    """Build flintd Go binary, or fall back to assets/ pre-built binary."""
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    prebuilt = os.path.join(project_root, "assets", "flintd")
    if os.path.exists(prebuilt):
        shutil.copy2(prebuilt, output_path)
        log.info("Using pre-built flintd from assets/")
        return

    source_dir = os.path.join(project_root, "guest", "flintd")
    if not os.path.isdir(source_dir):
        raise FileNotFoundError(f"flintd source not found: {source_dir}")

    env = os.environ.copy()
    env["CGO_ENABLED"] = "0"
    env["GOOS"] = "linux"
    env["GOARCH"] = "amd64"
    subprocess.run(
        ["go", "build", "-ldflags=-s -w", "-o", output_path, "."],
        cwd=source_dir,
        check=True,
        capture_output=True,
        env=env,
    )
    log.info("Built flintd from %s", source_dir)


def build_template(
    name: str,
    image_ref: str,
    *,
    rootfs_size_mb: int = 500,
    inject_flint: bool = True,
) -> str:
    """Full build pipeline: OCI pull -> rootfs extraction -> golden snapshot -> register.

    Pulls the OCI image referenced by *image_ref* (e.g., ``ubuntu:22.04``
    or ``ghcr.io/jverre/flint/base:latest``), extracts it into an ext4
    rootfs, creates a Firecracker snapshot, and registers the template.

    If *inject_flint* is True, the flintd guest agent and init-net.sh
    are injected into the rootfs. Set to False for images that already
    include them (e.g., pre-built Flint base images from ghcr.io).

    Returns template_id.
    """
    template_id = _slugify(name)
    template_dir = f"{TEMPLATES_DIR}/{template_id}"
    os.makedirs(template_dir, exist_ok=True)

    # Register as "building"
    register_template_artifact(
        template_id,
        name,
        "linux-firecracker",
        template_dir,
        status="building",
        rootfs_size_mb=rootfs_size_mb,
        image_ref=image_ref,
    )

    try:
        rootfs_path = f"{template_dir}/rootfs.ext4"

        # Check OCI cache — skip pull if digest matches
        digest = _oci_pull.resolve_digest(image_ref)
        cached = _oci_cache.get(image_ref, digest)

        if cached:
            _oci_cache.copy_cached_rootfs(cached, rootfs_path)
            log.info("Template %s: using cached rootfs (digest=%s)", template_id, digest[:16])
        else:
            # Pull and extract OCI image
            digest = _oci_pull.pull_and_extract(
                image_ref,
                rootfs_path,
                size_mb=rootfs_size_mb,
                inject_flint=inject_flint,
            )
            # Store in cache
            _oci_cache.put(image_ref, digest, rootfs_path)
            _oci_cache.cleanup()

        # Create golden snapshot for this template
        ns_name = f"fc-tmpl-{template_id[:12]}"
        tap_name = f"tap-tmpl-{template_id[:8]}"
        create_golden_snapshot(
            source_rootfs=rootfs_path,
            snapshot_dir=template_dir,
            ns_name=ns_name,
            tap_name=tap_name,
        )

        # Update status with digest
        update_template_artifact_status(template_id, "linux-firecracker", "ready")

        log.info("Template %s built successfully (image=%s)", template_id, image_ref)
        return template_id

    except Exception:
        update_template_artifact_status(template_id, "linux-firecracker", "failed")
        raise

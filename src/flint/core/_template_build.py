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
from . import _image_build


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
    *,
    image_ref: str | None = None,
    dockerfile: str | None = None,
    rootfs_size_mb: int = 500,
    inject_flint: bool = True,
) -> str:
    """Full build pipeline: obtain rootfs → snapshot → register.

    Exactly one of *image_ref* or *dockerfile* must be provided.

    * *image_ref*: pull the OCI image from a registry via crane (no Docker).
    * *dockerfile*: build locally using the first available builder
      (docker/podman/buildah) and export the resulting rootfs. Raises
      :class:`_image_build.NoBuilderError` if none is installed.

    If *inject_flint* is True, the flintd guest agent and init-net.sh are
    injected into the rootfs. Pre-built Flint images should pass False.

    Returns template_id.
    """
    if (image_ref is None) == (dockerfile is None):
        raise ValueError("Pass exactly one of image_ref or dockerfile")

    template_id = _slugify(name)
    template_dir = f"{TEMPLATES_DIR}/{template_id}"
    os.makedirs(template_dir, exist_ok=True)

    registry_image_ref = image_ref or f"dockerfile:{_image_build.dockerfile_hash(dockerfile)}"

    register_template_artifact(
        template_id,
        name,
        "linux-firecracker",
        template_dir,
        status="building",
        rootfs_size_mb=rootfs_size_mb,
        image_ref=registry_image_ref,
    )

    try:
        rootfs_path = f"{template_dir}/rootfs.ext4"

        if image_ref is not None:
            # ── OCI registry pull path ───────────────────────────────────
            digest = _oci_pull.resolve_digest(image_ref)
            cached = _oci_cache.get(image_ref, digest)

            if cached:
                _oci_cache.copy_cached_rootfs(cached, rootfs_path)
                log.info("Template %s: using cached rootfs (digest=%s)", template_id, digest[:16])
            else:
                digest = _oci_pull.pull_and_extract(
                    image_ref,
                    rootfs_path,
                    size_mb=rootfs_size_mb,
                    inject_flint=inject_flint,
                )
                _oci_cache.put(image_ref, digest, rootfs_path)
                _oci_cache.cleanup()
        else:
            # ── Local Dockerfile build path ──────────────────────────────
            dfile_hash = _image_build.dockerfile_hash(dockerfile)
            cache_key = f"dockerfile:{dfile_hash}"
            cached = _oci_cache.get(cache_key, dfile_hash)

            if cached:
                _oci_cache.copy_cached_rootfs(cached, rootfs_path)
                log.info("Template %s: using cached Dockerfile rootfs (hash=%s)", template_id, dfile_hash)
            else:
                tarball = _image_build.build_dockerfile_to_rootfs_tar(
                    dockerfile, tag_hint=f"flint-{template_id}",
                )
                try:
                    _oci_pull.extract_tar_to_rootfs(
                        tarball,
                        rootfs_path,
                        size_mb=rootfs_size_mb,
                        inject_flint=inject_flint,
                    )
                finally:
                    if os.path.exists(tarball):
                        os.remove(tarball)
                _oci_cache.put(cache_key, dfile_hash, rootfs_path)
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

        update_template_artifact_status(template_id, "linux-firecracker", "ready")

        log.info("Template %s built successfully (%s)", template_id, registry_image_ref)
        return template_id

    except Exception:
        update_template_artifact_status(template_id, "linux-firecracker", "failed")
        raise

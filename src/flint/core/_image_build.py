"""Local OCI image builds via docker/podman/buildah.

Enables the fluent Template API (``apt_install``, ``pip_install``,
``from_dockerfile``, ...) on top of the OCI extract pipeline. The runtime
does not require Docker — only template *builds* that use the fluent API
do. Users who reference pre-built images via ``from_oci_image()`` never
hit this path.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile

from .config import log

# Priority order: prefer daemonless builders when available.
SUPPORTED_BUILDERS = ("podman", "buildah", "docker")


class NoBuilderError(RuntimeError):
    """Raised when a Dockerfile build is requested but no builder is installed."""


def detect_builder() -> str | None:
    """Return the name of the first available image builder, or None."""
    for tool in SUPPORTED_BUILDERS:
        if shutil.which(tool):
            return tool
    return None


def dockerfile_hash(dockerfile: str, base_image: str | None = None) -> str:
    """Stable 12-char hash of Dockerfile + base image, for caching."""
    h = hashlib.sha256()
    if base_image:
        h.update(base_image.encode())
        h.update(b"\n")
    h.update(dockerfile.encode())
    return h.hexdigest()[:12]


def build_dockerfile_to_rootfs_tar(dockerfile: str, *, tag_hint: str = "flint-build") -> str:
    """Build a Dockerfile and export the flattened rootfs as a tarball.

    Returns a path to a tar file containing the merged filesystem of the
    resulting image. Caller is responsible for deleting the tarball when
    done.

    Raises :class:`NoBuilderError` if no builder (docker/podman/buildah)
    is available on the host.
    """
    builder = detect_builder()
    if not builder:
        raise NoBuilderError(
            "No container image builder found on host. Install docker, podman, "
            "or buildah — or pass a pre-built image with Template.from_oci_image()."
        )

    digest = dockerfile_hash(dockerfile)
    tag = f"{tag_hint}:{digest}"
    log.info("Building image with %s (tag=%s)", builder, tag)

    ctx = tempfile.mkdtemp(prefix="flint-build-ctx-")
    tarball_path = tempfile.mktemp(prefix="flint-rootfs-", suffix=".tar")
    try:
        dockerfile_path = os.path.join(ctx, "Dockerfile")
        with open(dockerfile_path, "w") as f:
            f.write(dockerfile)

        if builder == "buildah":
            subprocess.run(
                ["buildah", "bud", "-t", tag, "-f", dockerfile_path, ctx],
                check=True,
            )
        else:  # docker or podman
            subprocess.run(
                [builder, "build", "-t", tag, "-f", dockerfile_path, ctx],
                check=True,
            )

        # Export the flattened rootfs. Create an ephemeral container, export
        # its filesystem, then remove the container. Same pattern as the
        # old Docker-based template pipeline — but isolated to this call.
        cid_result = subprocess.run(
            [builder, "create", tag],
            check=True, capture_output=True, text=True,
        )
        cid = cid_result.stdout.strip().splitlines()[-1]
        try:
            subprocess.run(
                [builder, "export", "-o", tarball_path, cid],
                check=True,
            )
        finally:
            subprocess.run(
                [builder, "rm", cid],
                check=False, capture_output=True,
            )
    except Exception:
        # Make sure we don't leak a partial tarball on failure
        if os.path.exists(tarball_path):
            os.remove(tarball_path)
        raise
    finally:
        shutil.rmtree(ctx, ignore_errors=True)

    log.info("Image exported to %s", tarball_path)
    return tarball_path

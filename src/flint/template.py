"""Public Template API for Flint — OCI image-based builder interface."""

from __future__ import annotations

import time
from dataclasses import dataclass

from flint._client.client import DaemonClient


@dataclass
class TemplateInfo:
    template_id: str
    name: str
    status: str


def _get_client() -> DaemonClient:
    return DaemonClient()


class Template:
    """Builder for custom sandbox templates from OCI container images.

    Usage::

        template = (
            Template("python-data-science")
            .from_oci_image("python:3.12-slim")
            .build()
        )

        sandbox = Sandbox(template_id=template.template_id)

    For pre-built Flint images (that already contain flintd)::

        template = (
            Template("custom-base")
            .from_oci_image("ghcr.io/jverre/flint/base:latest", inject_flint=False)
            .build()
        )
    """

    def __init__(self, name: str, rootfs_size_mb: int = 500) -> None:
        self._name = name
        self._rootfs_size_mb = rootfs_size_mb
        self._image_ref: str | None = None
        self._inject_flint: bool = True

    # ── Image methods ──────────────────────────────────────────────────

    def from_oci_image(self, image_ref: str, *, inject_flint: bool = True) -> Template:
        """Pull an OCI image directly from any registry.

        If *inject_flint* is False, the image must already contain flintd
        and init-net.sh (e.g., pre-built Flint base images from ghcr.io).
        """
        self._image_ref = image_ref
        self._inject_flint = inject_flint
        return self

    def from_ubuntu_image(self, tag: str = "22.04") -> Template:
        self._image_ref = f"ubuntu:{tag}"
        return self

    def from_python_image(self, tag: str = "3.11-slim") -> Template:
        self._image_ref = f"python:{tag}"
        return self

    def from_node_image(self, tag: str = "20-slim") -> Template:
        self._image_ref = f"node:{tag}"
        return self

    def from_alpine_image(self, tag: str = "3.19") -> Template:
        self._image_ref = f"alpine:{tag}"
        return self

    def from_image(self, image: str) -> Template:
        self._image_ref = image
        return self

    # ── Build ───────────────────────────────────────────────────────────────

    def build(self, poll_interval: float = 2.0) -> TemplateInfo:
        """Build the template via the daemon. Blocks until complete."""
        if self._image_ref is None:
            raise ValueError("No image set. Call from_oci_image(), from_ubuntu_image(), etc. first.")

        client = _get_client()
        try:
            result = client.build_template(
                name=self._name,
                image_ref=self._image_ref,
                rootfs_size_mb=self._rootfs_size_mb,
                inject_flint=self._inject_flint,
            )
            template_id = result["template_id"]

            # Poll until build completes
            while True:
                info = client.get_template(template_id)
                if info is None:
                    raise RuntimeError(f"Template {template_id} disappeared during build")
                status = info.get("status", "unknown")
                if status == "ready":
                    return TemplateInfo(
                        template_id=template_id,
                        name=self._name,
                        status="ready",
                    )
                if status == "failed":
                    raise RuntimeError(f"Template build failed: {template_id}")
                time.sleep(poll_interval)
        finally:
            client.close()

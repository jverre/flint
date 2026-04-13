"""Public Template API for Flint — OCI image-based builder interface."""

from __future__ import annotations

import shlex
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
    """Builder for custom sandbox templates.

    Two build modes:

    1. **Pull a pre-built OCI image** (no builder required on host)::

        template = (
            Template("my-env")
            .from_oci_image("python:3.11-slim")
            .build()
        )

    2. **Declarative build on top of a base image** — requires
       ``docker``, ``podman``, or ``buildah`` on the host. Each operation
       (``apt_install``, ``pip_install``, ``run_cmd``, ...) appends a
       layer to a Dockerfile that is built locally, exported as a flat
       rootfs, and snapshotted::

        template = (
            Template("python-data-science")
            .from_python_image("3.11-slim")
            .apt_install("git", "curl")
            .pip_install("numpy", "pandas")
            .set_workdir("/workspace")
            .build()
        )

    If you don't have a local image builder and try to use the fluent ops,
    ``build()`` raises a clear error. Pre-built images via ``from_oci_image``
    always work.
    """

    def __init__(self, name: str, rootfs_size_mb: int = 500) -> None:
        self._name = name
        self._rootfs_size_mb = rootfs_size_mb
        self._base_image: str | None = None
        self._dockerfile_override: str | None = None
        self._ops: list[str] = []
        self._inject_flint: bool = True

    # ── Base image methods ─────────────────────────────────────────────────

    def from_oci_image(self, image_ref: str, *, inject_flint: bool = True) -> Template:
        """Pull an OCI image directly from any registry.

        If *inject_flint* is False, the image must already contain flintd
        and ``/etc/init-net.sh`` (e.g., pre-built Flint base images).
        """
        self._base_image = image_ref
        self._inject_flint = inject_flint
        return self

    def from_ubuntu_image(self, tag: str = "22.04") -> Template:
        self._base_image = f"ubuntu:{tag}"
        return self

    def from_python_image(self, tag: str = "3.11-slim") -> Template:
        self._base_image = f"python:{tag}"
        return self

    def from_node_image(self, tag: str = "20-slim") -> Template:
        self._base_image = f"node:{tag}"
        return self

    def from_alpine_image(self, tag: str = "3.19") -> Template:
        self._base_image = f"alpine:{tag}"
        return self

    def from_image(self, image: str) -> Template:
        """Alias for :meth:`from_oci_image` (backwards compat)."""
        self._base_image = image
        return self

    def from_dockerfile(self, dockerfile: str) -> Template:
        """Use a complete Dockerfile as-is.

        Mutually exclusive with fluent ops (``apt_install`` etc.) and
        base-image selectors. Requires docker/podman/buildah on the host.
        """
        self._dockerfile_override = dockerfile
        return self

    # ── Operation methods (require local builder) ─────────────────────────

    def apt_install(self, *packages: str) -> Template:
        pkgs = " ".join(shlex.quote(p) for p in packages)
        self._ops.append(
            f"RUN apt-get update && apt-get install -y --no-install-recommends {pkgs} "
            f"&& rm -rf /var/lib/apt/lists/*"
        )
        return self

    def apk_install(self, *packages: str) -> Template:
        pkgs = " ".join(shlex.quote(p) for p in packages)
        self._ops.append(f"RUN apk add --no-cache {pkgs}")
        return self

    def pip_install(self, *packages: str) -> Template:
        pkgs = " ".join(shlex.quote(p) for p in packages)
        self._ops.append(f"RUN pip install --no-cache-dir {pkgs}")
        return self

    def npm_install(self, *packages: str) -> Template:
        pkgs = " ".join(shlex.quote(p) for p in packages)
        self._ops.append(f"RUN npm install -g {pkgs}")
        return self

    def run_cmd(self, cmd: str) -> Template:
        self._ops.append(f"RUN {cmd}")
        return self

    def set_workdir(self, path: str) -> Template:
        self._ops.append(f"WORKDIR {path}")
        return self

    def set_envs(self, **envs: str) -> Template:
        for k, v in envs.items():
            self._ops.append(f'ENV {k}="{v}"')
        return self

    def git_clone(self, repo: str, dest: str) -> Template:
        self._ops.append(f"RUN git clone {shlex.quote(repo)} {shlex.quote(dest)}")
        return self

    # ── Build ──────────────────────────────────────────────────────────────

    def _assemble_dockerfile(self) -> str | None:
        """Return a Dockerfile string if any ops were added, else None.

        When None is returned, the build can go through the pure OCI-pull
        path (no local builder required).
        """
        if self._dockerfile_override is not None:
            return self._dockerfile_override
        if not self._ops:
            return None
        if not self._base_image:
            raise ValueError(
                "No base image set. Call from_*_image() before adding operations."
            )
        return "\n".join([f"FROM {self._base_image}", *self._ops]) + "\n"

    def build(self, poll_interval: float = 2.0) -> TemplateInfo:
        """Build the template via the daemon. Blocks until complete."""
        dockerfile = self._assemble_dockerfile()
        if dockerfile is None and self._base_image is None:
            raise ValueError(
                "No image set. Call from_oci_image() or from_*_image() first."
            )

        client = _get_client()
        try:
            if dockerfile is not None:
                result = client.build_template(
                    self._name,
                    dockerfile=dockerfile,
                    rootfs_size_mb=self._rootfs_size_mb,
                    inject_flint=self._inject_flint,
                )
            else:
                result = client.build_template(
                    self._name,
                    image_ref=self._base_image,
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

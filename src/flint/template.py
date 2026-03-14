"""Public Template API for Flint — E2B-style builder interface."""

from __future__ import annotations

import time
from dataclasses import dataclass

from flint._client.client import DaemonClient
from flint.core.config import DAEMON_URL


@dataclass
class TemplateInfo:
    template_id: str
    name: str
    status: str


def _get_client() -> DaemonClient:
    return DaemonClient()


class Template:
    """Fluent builder for custom sandbox templates.

    Usage::

        template = (
            Template("python-data-science")
            .from_ubuntu_image("22.04")
            .apt_install("python3", "python3-pip")
            .pip_install("numpy", "pandas")
            .set_workdir("/workspace")
            .build()
        )

        sandbox = Sandbox(template_id=template.template_id)
    """

    def __init__(self, name: str, rootfs_size_mb: int = 500) -> None:
        self._name = name
        self._rootfs_size_mb = rootfs_size_mb
        self._base_image: str | None = None
        self._steps: list[dict] = []
        self._dockerfile: str | None = None

    # ── Base image methods ──────────────────────────────────────────────────

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
        self._base_image = image
        return self

    def from_dockerfile(self, dockerfile: str) -> Template:
        self._dockerfile = dockerfile
        return self

    # ── Operation methods ───────────────────────────────────────────────────

    def apt_install(self, *packages: str) -> Template:
        self._steps.append({"type": "apt_install", "packages": list(packages)})
        return self

    def pip_install(self, *packages: str) -> Template:
        self._steps.append({"type": "pip_install", "packages": list(packages)})
        return self

    def npm_install(self, *packages: str) -> Template:
        self._steps.append({"type": "npm_install", "packages": list(packages)})
        return self

    def run_cmd(self, cmd: str) -> Template:
        self._steps.append({"type": "run", "cmd": cmd})
        return self

    def copy(self, src: str, dest: str) -> Template:
        self._steps.append({"type": "copy", "src": src, "dest": dest})
        return self

    def set_workdir(self, path: str) -> Template:
        self._steps.append({"type": "workdir", "path": path})
        return self

    def set_envs(self, **envs: str) -> Template:
        self._steps.append({"type": "env", "envs": envs})
        return self

    def git_clone(self, repo: str, dest: str = "") -> Template:
        self._steps.append({"type": "git_clone", "repo": repo, "dest": dest})
        return self

    # ── Build ───────────────────────────────────────────────────────────────

    def _to_dockerfile(self) -> str:
        if self._dockerfile is not None:
            return self._dockerfile
        if self._base_image is None:
            raise ValueError("No base image set. Call from_ubuntu_image(), from_image(), etc. first.")
        from flint.core._template_build import _generate_dockerfile
        return _generate_dockerfile(self._base_image, self._steps)

    def build(self, poll_interval: float = 2.0) -> TemplateInfo:
        """Build the template via the daemon. Blocks until complete."""
        dockerfile = self._to_dockerfile()
        client = _get_client()
        try:
            result = client.build_template(
                name=self._name,
                dockerfile=dockerfile,
                rootfs_size_mb=self._rootfs_size_mb,
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

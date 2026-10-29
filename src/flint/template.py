"""Public Template API for Flint: container images that sandboxes boot from."""

from __future__ import annotations

import time
from dataclasses import dataclass

from flint.sandbox import _get_client


@dataclass(frozen=True)
class Template:
    """A root disk and golden snapshot made from a container image.

    Build and push the image with your usual tooling, then import it once (an
    admin token is required). Every sandbox created from it boots in
    milliseconds::

        from flint import Sandbox, Template

        t = Template.import_image("python", "python:3.12-slim")
        sandbox = Sandbox(template_id=t.template_id)

    Flint pins the tag to the digest it pulled (``t.digest``), so a template
    never changes when the tag moves; import again to update it. Without
    ``tenant`` every tenant can boot the template; with one, only that tenant.
    """

    template_id: str
    name: str
    status: str
    tenant: str = ""
    image: str | None = None
    digest: str | None = None
    error: str | None = None

    @classmethod
    def import_image(cls, name: str, image: str, *, rootfs_size_mb: int = 2048, tenant: str | None = None,
                     registry_auth: dict | None = None, timeout: float = 1200.0,
                     poll_interval: float = 2.0) -> Template:
        """Import ``image`` as template ``name`` and wait until it is ready.

        ``registry_auth`` (``{"username": ..., "password": ...}``) is for private
        registries; Flint uses it for this pull only and never stores it.
        ``rootfs_size_mb`` is the template's disk; the image must fit in it.
        """
        client = _get_client()
        template_id = client.import_template(name, image, rootfs_size_mb=rootfs_size_mb, tenant=tenant,
                                             registry_auth=registry_auth)["template_id"]
        deadline = time.monotonic() + timeout
        while True:
            # The import runs in the background; the template may take a
            # moment to appear, so None means "not registered yet".
            info = client.get_template(template_id)
            if info is not None:
                if info.get("status") == "ready":
                    return cls._from(info)
                if info.get("status") in ("failed", "error"):  # a daemon says failed, a cluster error
                    raise RuntimeError(f"Importing template {template_id} failed: {info.get('error') or 'see the daemon log'}")
            if time.monotonic() > deadline:
                raise TimeoutError(f"Importing template {template_id} took longer than {timeout:.0f}s")
            time.sleep(poll_interval)

    @classmethod
    def get(cls, template_id: str) -> Template | None:
        info = _get_client().get_template(template_id)
        return cls._from(info) if info is not None else None

    @classmethod
    def list(cls) -> list[Template]:
        return [cls._from(t) for t in _get_client().list_templates()]

    def delete(self) -> None:
        """Delete the template. Running sandboxes keep working (admin only)."""
        _get_client().delete_template(self.template_id)

    @classmethod
    def _from(cls, d: dict) -> Template:
        return cls(template_id=d["template_id"], name=d.get("name", d["template_id"]), status=d.get("status", ""),
                   tenant=d.get("tenant") or "", image=d.get("image"), digest=d.get("digest"), error=d.get("error"))

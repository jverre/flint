"""Public Volume API for Flint: shared directories you mount into sandboxes."""

from __future__ import annotations

from dataclasses import dataclass

from flint.sandbox import _get_client


@dataclass(frozen=True)
class Volume:
    """A shared directory on the host that sandboxes mount while running.

    Usage::

        from flint import Sandbox, Volume

        results = Volume.create("results")
        parent = Sandbox()
        parent.mount(results, "/results", readonly=True)

        child = Sandbox()
        child.mount(results, "/out", subpath=child.id)   # child writes /out/...
        # parent sees it under /results/<child id>/

    A read-write mount owns its subtree: another read-write mount of an
    overlapping subpath is refused. Read-only mounts are unlimited.
    """

    id: str
    name: str

    @classmethod
    def create(cls, name: str) -> Volume:
        return cls._from(_get_client().create_volume(name))

    @classmethod
    def get(cls, name: str) -> Volume | None:
        """Find a volume by name or ID."""
        return next((v for v in cls.list() if name in (v.id, v.name)), None)

    @classmethod
    def list(cls) -> list[Volume]:
        return [cls._from(v) for v in _get_client().list_volumes()]

    def delete(self) -> None:
        """Delete the volume and its files. Fails while it is mounted."""
        _get_client().delete_volume(self.id)

    @classmethod
    def _from(cls, d: dict) -> Volume:
        return cls(id=d["id"], name=d["name"])

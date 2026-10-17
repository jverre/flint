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

    Volumes are block images where the host supports it (``kind == "image"``):
    they have a size (sparse, default 10 GiB) and can be copied cheaply::

        snap = results.snapshot("results-v1")   # read-only, frozen now
        work = results.fork("results-try-2")     # writable, independent
    """

    id: str
    name: str
    kind: str = "dir"
    size_gib: int | None = None
    readonly: bool = False

    @classmethod
    def create(cls, name: str, size_gib: int | None = None) -> Volume:
        return cls._from(_get_client().create_volume(name, size_gib))

    def snapshot(self, name: str) -> Volume:
        """A read-only copy of this volume as it is now; it shares every block."""
        return self._from(_get_client().snapshot_volume(self.id, name))

    def fork(self, name: str) -> Volume:
        """A writable copy of this volume as it is now, independent from then on."""
        return self._from(_get_client().fork_volume(self.id, name))

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
        return cls(id=d["id"], name=d["name"], kind=d.get("kind", "dir"),
                   size_gib=d.get("size_gib"), readonly=bool(d.get("readonly")))

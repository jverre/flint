"""Pluggable storage backends for sandbox filesystems.

Usage::

    from flint.core.storage import get_storage_backend

    backend = get_storage_backend()   # reads FLINT_STORAGE_BACKEND env var
    backend = get_storage_backend("r2")  # explicit override
"""

from __future__ import annotations

from ..config import STORAGE_BACKEND
from .base import StorageBackend
from .local import LocalStorageBackend
from .r2 import R2StorageBackend
from .s3 import S3FilesStorageBackend

_BACKENDS: dict[str, type[StorageBackend]] = {
    "local": LocalStorageBackend,
    "s3_files": S3FilesStorageBackend,
    "r2": R2StorageBackend,
}


def get_storage_backend(kind: str | None = None) -> StorageBackend:
    """Create a storage backend instance.

    Args:
        kind: Backend name (``"local"``, ``"s3_files"``, ``"r2"``).
              Defaults to the ``FLINT_STORAGE_BACKEND`` environment variable.

    Returns:
        An initialized (but not yet started) :class:`StorageBackend`.
    """
    kind = kind or STORAGE_BACKEND
    cls = _BACKENDS.get(kind)
    if cls is None:
        available = ", ".join(sorted(_BACKENDS))
        raise ValueError(f"Unknown storage backend: {kind!r} (available: {available})")
    return cls()


__all__ = [
    "StorageBackend",
    "LocalStorageBackend",
    "S3FilesStorageBackend",
    "R2StorageBackend",
    "get_storage_backend",
]

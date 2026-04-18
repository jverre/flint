"""Flint backend plugin system.

Built-in backends register themselves via :mod:`.registry` side-effects.
Third-party backends register via the ``flint.backends`` entry-point group.
Use :func:`resolve_backend` to build a backend by name (or ``None`` for the
host default).
"""

from __future__ import annotations

import os

from .api import BackendBootResult, BackendPlugin, HostBackend
from .registry import (
    BackendInfo,
    BackendNotFound,
    available,
    default_for_host,
    get_backend,
    names,
    register,
)

__all__ = [
    "BackendBootResult",
    "BackendInfo",
    "BackendNotFound",
    "BackendPlugin",
    "HostBackend",
    "available",
    "default_for_host",
    "get_backend",
    "get_host_backend",
    "names",
    "register",
    "resolve_backend",
]


def resolve_backend(explicit: str | None = None) -> BackendPlugin:
    """Return a backend instance.

    Resolution order:

    1. ``explicit`` argument if set.
    2. ``FLINT_BACKEND`` environment variable if set.
    3. :func:`registry.default_for_host`.

    Raises :class:`BackendNotFound` if the selected name is unknown, or
    :class:`RuntimeError` if no backend matches the current host.
    """
    name = explicit or os.environ.get("FLINT_BACKEND") or default_for_host()
    if not name:
        raise RuntimeError(
            "No Flint backend is available for this host. Install one of the "
            "optional extras (e.g. `pip install flint[firecracker]`) or set "
            "FLINT_BACKEND explicitly."
        )
    return get_backend(name)


def get_host_backend() -> BackendPlugin:
    """Backward-compatible shim: delegates to :func:`resolve_backend`."""
    return resolve_backend()

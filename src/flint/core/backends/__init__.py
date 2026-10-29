"""Flint backend plugin system.

Built-in backends register themselves via :mod:`.registry` side-effects.
Third-party backends register via the ``flint.backends`` entry-point group.
Use :func:`resolve_backend` to build a backend by name (or ``None`` for the
host default). The contract every backend implements is
:class:`~flint.core.backends.base.BackendPlugin`; anything hypervisor-specific
stays in the backend (and ``BootResult.metadata``), so the rest of core never
knows about a particular backend's internals.
"""

from __future__ import annotations

import os

from .base import BackendPlugin, BootResult, RecoveryState
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
    "BackendInfo",
    "BackendNotFound",
    "BackendPlugin",
    "BootResult",
    "RecoveryState",
    "available",
    "default_for_host",
    "get_backend",
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


"""Plugin surface for Flint backends.

Re-exports `BackendPlugin` (the ABC all backends implement) and
`BackendBootResult`. Third-party backends are discovered via the
``flint.backends`` entry-point group (see `registry.py`).

The abstract contract mirrors what the daemon actually calls; anything
hypervisor-specific (sockets, chroot paths, namespace names) goes into
``BackendBootResult.backend_metadata`` and `_SandboxEntry.backend_metadata`
so that flint-core never knows about a particular backend's internals.
"""

from __future__ import annotations

from .base import BackendBootResult, BackendPlugin, HostBackend, RecoveryState

__all__ = [
    "BackendBootResult",
    "BackendPlugin",
    "HostBackend",
    "RecoveryState",
]

"""Backend registry.

Backends are looked up by ``kind`` (``"linux-firecracker"``, ``"macos-vz-arm64"``,
``"v8-isolate"``, ...). The registry returns cached instances. Each kind is
lazy-imported so e.g. macOS hosts don't pay the cost of loading the Firecracker
backend module.
"""

from __future__ import annotations

import platform
from typing import Callable

from flint.errors import BackendUnavailable

from .base import Backend


def _load_linux_firecracker() -> Backend:
    from .linux_firecracker import LinuxFirecrackerBackend
    return LinuxFirecrackerBackend()


def _load_macos_vz() -> Backend:
    from .macos_vz import MacOSVirtualizationBackend
    return MacOSVirtualizationBackend()


_FACTORIES: dict[str, Callable[[], Backend]] = {
    "linux-firecracker": _load_linux_firecracker,
    "macos-vz-arm64": _load_macos_vz,
}


_INSTANCES: dict[str, Backend] = {}


def register_backend(kind: str, factory: Callable[[], Backend]) -> None:
    """Register a backend factory under ``kind``. Useful for tests + plugins."""
    _FACTORIES[kind] = factory


def registered_kinds() -> list[str]:
    return sorted(_FACTORIES)


def get_backend(kind: str) -> Backend:
    """Return a cached backend instance for ``kind``."""
    if kind not in _FACTORIES:
        raise BackendUnavailable(kind, f"unknown backend (registered: {registered_kinds()})")
    inst = _INSTANCES.get(kind)
    if inst is None:
        inst = _FACTORIES[kind]()
        _INSTANCES[kind] = inst
    return inst


def default_backend_kind() -> str:
    """Return the kind to use when the caller hasn't picked one explicitly."""
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Linux":
        return "linux-firecracker"
    if system == "Darwin" and machine == "arm64":
        return "macos-vz-arm64"
    raise BackendUnavailable("<auto>", f"no default backend for host {system} {machine}")


def available_backends() -> list[str]:
    """Return registered kinds that successfully construct on this host."""
    out = []
    for kind in registered_kinds():
        try:
            get_backend(kind)
            out.append(kind)
        except Exception:
            continue
    return out


__all__ = [
    "Backend",
    "register_backend",
    "registered_kinds",
    "get_backend",
    "default_backend_kind",
    "available_backends",
]

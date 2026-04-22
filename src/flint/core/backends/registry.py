"""Plugin registry for Flint backends.

Backends register themselves by name. Built-in backends (firecracker,
cloud-hypervisor, macos-vz) register via module-level side effects in
:func:`_load_builtins`. Third-party backends register via the
``flint.backends`` entry-point group in their own distribution's metadata::

    [project.entry-points."flint.backends"]
    my-backend = "my_pkg:MyBackend"

Any class returned from an entry point must be a subclass of
:class:`BackendPlugin` and must set the ``name`` / ``kind`` /
``supported_platforms`` class attributes.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import platform
import threading
from dataclasses import dataclass
from typing import Type

from .base import BackendPlugin


class BackendNotFound(KeyError):
    """Raised when :func:`get_backend` can't find a matching plugin."""


@dataclass(frozen=True)
class BackendInfo:
    name: str
    kind: str
    display_name: str
    supported_platforms: tuple[str, ...]
    preflight_ok: bool
    preflight_problems: tuple[str, ...]


_lock = threading.Lock()
_by_name: dict[str, Type[BackendPlugin]] = {}
_by_kind: dict[str, Type[BackendPlugin]] = {}
_discovered = False


def register(plugin_cls: Type[BackendPlugin]) -> None:
    """Register ``plugin_cls`` under its ``name`` and ``kind`` attributes.

    Repeated registration with the same class is a no-op. Registering a
    different class under a name that's already taken raises ``ValueError``.
    """
    if not isinstance(plugin_cls, type) or not issubclass(plugin_cls, BackendPlugin):
        raise TypeError(
            f"{plugin_cls!r} is not a subclass of BackendPlugin"
        )
    name = getattr(plugin_cls, "name", None)
    kind = getattr(plugin_cls, "kind", None)
    if not name or not kind:
        raise ValueError(
            f"Backend {plugin_cls.__name__} must set both `name` and `kind` class attributes"
        )
    with _lock:
        existing = _by_name.get(name)
        if existing is not None and existing is not plugin_cls:
            raise ValueError(
                f"Backend name {name!r} already registered by {existing.__name__}"
            )
        _by_name[name] = plugin_cls
        _by_kind[kind] = plugin_cls


def _load_builtins() -> None:
    # Each import triggers a top-level register() call.
    try:
        importlib.import_module("flint.core.backends.linux_firecracker")
    except Exception:  # pragma: no cover - optional backend
        pass
    try:
        importlib.import_module("flint.core.backends.linux_cloud_hypervisor")
    except Exception:  # pragma: no cover - optional backend
        pass
    try:
        importlib.import_module("flint.core.backends.macos_vz")
    except Exception:  # pragma: no cover - optional backend
        pass


def _load_entry_points() -> None:
    try:
        eps = importlib.metadata.entry_points(group="flint.backends")
    except Exception:  # pragma: no cover - older Python
        return
    for ep in eps:
        try:
            plugin_cls = ep.load()
        except Exception:  # pragma: no cover - third-party plugin failure
            continue
        try:
            register(plugin_cls)
        except Exception:  # pragma: no cover
            continue


def _discover() -> None:
    global _discovered
    with _lock:
        if _discovered:
            return
        _discovered = True
    _load_builtins()
    _load_entry_points()


def available() -> list[BackendInfo]:
    """Return metadata for every registered backend (discovery runs if needed)."""
    _discover()
    with _lock:
        classes = list(dict.fromkeys(_by_name.values()))
    infos: list[BackendInfo] = []
    for cls in classes:
        instance = cls()
        problems = tuple(instance.preflight())
        infos.append(
            BackendInfo(
                name=cls.name,
                kind=cls.kind,
                display_name=getattr(cls, "display_name", "") or cls.name,
                supported_platforms=tuple(getattr(cls, "supported_platforms", ())),
                preflight_ok=not problems,
                preflight_problems=problems,
            )
        )
    return sorted(infos, key=lambda i: i.name)


def names() -> list[str]:
    _discover()
    with _lock:
        return sorted(_by_name)


def get_backend(name: str) -> BackendPlugin:
    """Return a fresh instance of the named plugin.

    Accepts either the short ``name`` (e.g. ``"firecracker"``) or the longer
    ``kind`` (e.g. ``"linux-firecracker"``) so state-store rows keep resolving.
    Raises :class:`BackendNotFound` if no plugin matches.
    """
    _discover()
    with _lock:
        cls = _by_name.get(name) or _by_kind.get(name)
    if cls is None:
        raise BackendNotFound(
            f"unknown backend {name!r}; available: {', '.join(names()) or '(none)'}"
        )
    return cls()


def default_for_host() -> str | None:
    """Return the ``name`` of the best-fit backend for the current host.

    Picks the first registered plugin whose ``supported_platforms`` matches
    the running system and whose preflight passes. If none match, returns the
    first registered plugin whose platform list simply matches (even if
    preflight fails) — this lets the daemon start and report a clear error
    rather than "no backend installed".
    """
    _discover()
    system = platform.system().lower()
    machine = platform.machine().lower()
    here_short = system
    here_full = f"{system}-{machine}"
    with _lock:
        classes = list(dict.fromkeys(_by_name.values()))
    platform_matches: list[Type[BackendPlugin]] = []
    for cls in classes:
        plats = {p.lower() for p in getattr(cls, "supported_platforms", ())}
        if not plats or here_short in plats or here_full in plats:
            platform_matches.append(cls)
    for cls in platform_matches:
        if not cls().preflight():
            return cls.name
    if platform_matches:
        return platform_matches[0].name
    return None


def reset_for_tests() -> None:
    """Clear the registry — only intended for unit tests."""
    global _discovered
    with _lock:
        _by_name.clear()
        _by_kind.clear()
        _discovered = False

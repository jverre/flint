"""The VM manager and the backends it drives.

Exports load lazily, so importing a light module such as ``flint.core.config``
doesn't load the manager and everything it depends on.
"""

__all__ = ["SandboxManager", "CommandResult", "SandboxState"]


def __getattr__(name: str):
    if name == "SandboxManager":
        from .manager import SandboxManager
        return SandboxManager
    if name in ("CommandResult", "SandboxState"):
        from . import types
        return getattr(types, name)
    raise AttributeError(f"module 'flint.core' has no attribute {name!r}")

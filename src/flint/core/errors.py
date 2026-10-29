"""Errors the sandbox manager raises for requests it can't serve.

Each carries the HTTP ``status`` an API maps it to, so callers (the daemon's
exception handler, the privileged runtime's RPC boundary) translate them in
one place instead of guessing from a message. They subclass RuntimeError so
code that caught the bare RuntimeError the manager used to raise still works,
and are built from their message alone so they can cross the runtime RPC.
"""

from __future__ import annotations


class SandboxError(RuntimeError):
    """A request about a sandbox that can't be served as asked."""

    status: int | None = None  # the API answers 500

    @property
    def detail(self) -> str:
        return str(self)


class SandboxNotFound(SandboxError):
    """No sandbox with this ID (on this host)."""

    status = 404

    @classmethod
    def of(cls, sandbox_id: str, where: str = "") -> SandboxNotFound:
        return cls(f"Sandbox {sandbox_id} not found{where}")


class InvalidState(SandboxError):
    """The sandbox exists but its state doesn't allow the operation (e.g. pausing a paused VM)."""

    status = 409


class Conflict(SandboxError):
    """The operation conflicts with what already exists (e.g. restoring a VM that runs here)."""

    status = 409


# By class name, for rebuilding an error that crossed a process boundary.
BY_NAME: dict[str, type[SandboxError]] = {
    cls.__name__: cls for cls in (SandboxError, SandboxNotFound, InvalidState, Conflict)
}

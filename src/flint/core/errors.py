"""Errors for requests that can't be served as asked.

Each carries the HTTP ``status`` an API answers with, so the daemon maps
them to responses in one exception handler, and the privileged runtime
sends them over its socket as ``(status, detail, headers)``:
:func:`from_status` rebuilds the matching class on the other side. They
subclass RuntimeError so code that caught the bare RuntimeError the manager
used to raise still works.
"""

from __future__ import annotations


class FlintError(RuntimeError):
    """A request that can't be served. ``status`` None: the API answers 500."""

    status: int | None = None

    def __init__(self, detail: str = "", *, headers: dict[str, str] | None = None) -> None:
        super().__init__(detail)
        self.headers = headers

    @property
    def detail(self) -> str:
        return str(self)


class InvalidRequest(FlintError, ValueError):
    """The request is malformed or asks for something impossible."""

    status = 400


class Forbidden(FlintError):
    """The caller may not do this."""

    status = 403


class NotFound(FlintError):
    """No such resource (on this host)."""

    status = 404


class Conflict(FlintError):
    """The request conflicts with what exists (e.g. a template already owned by another tenant)."""

    status = 409


class Busy(FlintError):
    """Too much is going on (a limit or a queue is full); retry later."""

    status = 429


class Unsupported(FlintError):
    """This host's backend can't do it."""

    status = 501


class Unavailable(FlintError):
    """Can't be done right now (not ready, or a cleanup must be retried)."""

    status = 503


class HostFull(FlintError):
    """This host has no room for another VM; a cluster scheduler tries another."""

    status = 507


class SandboxError(FlintError):
    """A request about a sandbox that can't be served as asked."""


class SandboxNotFound(SandboxError, NotFound):
    """No sandbox with this ID (on this host)."""

    @classmethod
    def of(cls, sandbox_id: str, where: str = "") -> SandboxNotFound:
        return cls(f"Sandbox {sandbox_id} not found{where}")


class InvalidState(SandboxError, Conflict):
    """The sandbox exists but its state doesn't allow the operation (e.g. pausing a paused VM)."""


_BY_STATUS: dict[int, type[FlintError]] = {
    cls.status: cls for cls in (InvalidRequest, Forbidden, NotFound, Conflict, Busy, Unsupported, Unavailable, HostFull)
}


def from_status(status: int | None, detail: str, headers: dict[str, str] | None = None) -> FlintError:
    """The error to raise for one that crossed a process boundary as ``(status, detail, headers)``."""
    cls = _BY_STATUS.get(status) if status else None
    if cls is None:
        error = FlintError(detail, headers=headers)
        error.status = status
        return error
    return cls(detail, headers=headers)

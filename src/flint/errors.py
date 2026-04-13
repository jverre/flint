"""Public exception types raised by the flint SDK and daemon."""

from __future__ import annotations


class FlintError(Exception):
    """Base class for all flint-raised errors."""


class BackendCapabilityMissing(FlintError):
    """Raised when an operation requires a capability the backend doesn't provide.

    Carries structured fields so callers can branch programmatically:

        try:
            sandbox.commands.run("echo hi")
        except BackendCapabilityMissing as e:
            print(e.capability, e.backend)
    """

    def __init__(self, *, capability: str, backend: str, message: str | None = None) -> None:
        self.capability = capability
        self.backend = backend
        super().__init__(message or f"Backend {backend!r} does not support capability {capability!r}")


class SandboxNotFound(FlintError):
    """Raised when a sandbox id does not resolve to a live or paused entry."""

    def __init__(self, sandbox_id: str) -> None:
        self.sandbox_id = sandbox_id
        super().__init__(f"Sandbox {sandbox_id!r} not found")


class BackendUnavailable(FlintError):
    """Raised when a requested backend kind is not registered or not usable on this host."""

    def __init__(self, kind: str, reason: str | None = None) -> None:
        self.kind = kind
        self.reason = reason
        msg = f"Backend {kind!r} is not available"
        if reason:
            msg += f": {reason}"
        super().__init__(msg)

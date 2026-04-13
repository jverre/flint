"""Backend lifecycle interface.

Backends implement only lifecycle here. Operational capabilities (shell, files,
PTY, JS eval, etc.) are declared by also implementing the relevant Protocols
in :mod:`flint.core.backends.capabilities`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Mapping

from .capabilities import derive_capabilities


@dataclass
class BackendBootResult:
    """Returned by :meth:`Backend.create` and :meth:`SupportsPause.resume`.

    Backends populate the fields they care about; everything else stays at its
    default. The manager copies these onto the ``_SandboxEntry`` and persists
    them via ``StateStore``.
    """

    process: Any = None
    pid: int = -1
    vm_dir: str = ""
    socket_path: str = ""
    ns_name: str = ""
    guest_ip: str = ""
    agent_url: str = ""
    chroot_base: str = ""
    backend_vm_ref: str = ""
    runtime_dir: str = ""
    guest_arch: str = ""
    transport_ref: str = ""
    timings: dict[str, float] = field(default_factory=dict)
    t_total: float = 0.0
    backend_metadata: dict[str, Any] = field(default_factory=dict)


# Recovery liveness states returned by ``Backend.recover``.
ALIVE = "alive"
PAUSED = "paused"
DEAD = "dead"


class Backend(ABC):
    """Lifecycle contract every backend must satisfy.

    See :mod:`flint.core.backends.capabilities` for opt-in operational
    surface (shell, files, PTY, JS eval, ...).
    """

    kind: str

    @abstractmethod
    def ensure_runtime_ready(self) -> None:
        """Validate or initialize host-side prerequisites (binaries, kernel modules, etc)."""

    @abstractmethod
    def ensure_default_template(self) -> None:
        """Make sure the default template artifact for this backend exists on disk."""

    @abstractmethod
    def create(
        self,
        *,
        template_id: str,
        options: Mapping[str, Any] | None = None,
    ) -> BackendBootResult:
        """Boot a sandbox from a template. ``options`` is backend-specific."""

    @abstractmethod
    def kill(self, entry: Any) -> None:
        """Terminate a sandbox and clean up its resources."""

    @abstractmethod
    def health(self, entry: Any) -> tuple[bool, str | None]:
        """Return ``(alive, reason_or_None)`` for the running sandbox."""

    @abstractmethod
    def recover(self, row: dict) -> tuple[str, Any]:
        """After daemon restart, decide what to do with a persisted row.

        Returns ``(ALIVE, entry)``, ``(PAUSED, None)``, or ``(DEAD, None)``.
        """

    # Capabilities are auto-derived from the protocols this instance satisfies.
    @property
    def capabilities(self) -> frozenset[str]:
        return derive_capabilities(self)

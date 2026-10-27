from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, ClassVar, Literal

RecoveryState = Literal["alive", "paused", "dead"]


def row_backend_metadata(row: dict) -> dict:
    """Return the per-sandbox backend_metadata dict from a state-store row.

    Rows can carry it as either a pre-parsed dict (``backend_metadata``) or
    the raw ``backend_meta_json`` TEXT column. Accept either.
    """
    meta = row.get("backend_metadata")
    if isinstance(meta, dict):
        return meta
    raw = row.get("backend_meta_json")
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return {}


@dataclass
class BackendBootResult:
    """Return value of :meth:`BackendPlugin.create`/:meth:`BackendPlugin.resume`.

    Only ``agent_url``/``guest_ip`` and ``process``/``pid`` are part of the
    hypervisor-agnostic contract — everything else is either convenience for
    existing backends or lives in ``backend_metadata`` for backends that need
    to stash arbitrary per-VM state (socket path, chroot dir, namespace name…).
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


class BackendPlugin(ABC):
    """Contract every Flint backend implements.

    Subclasses set three class attributes:

    * ``name`` — short, stable, user-facing id (``"firecracker"``,
      ``"cloud-hypervisor"``, ``"macos-vz"``). Used by the CLI ``--backend``
      flag and by the plugin registry.
    * ``kind`` — longer identifier persisted on sandbox rows in the state DB
      (``"linux-firecracker"``, ``"linux-cloud-hypervisor"``,
      ``"macos-vz-arm64"``). Kept for backward compatibility with existing
      databases; the registry indexes plugins by both ``name`` and ``kind``.
    * ``supported_platforms`` — tuple of platform strings, each in the form
      ``"<system>"`` or ``"<system>-<machine>"`` (lowercase, e.g. ``"linux"``,
      ``"darwin-arm64"``). Empty tuple means "any platform".
    """

    name: ClassVar[str]
    kind: ClassVar[str]
    display_name: ClassVar[str] = ""
    supported_platforms: ClassVar[tuple[str, ...]] = ()
    # The credential proxy needs the VM's own network namespace.
    supports_network_policy: ClassVar[bool] = True

    def preflight(self) -> list[str]:
        """Check whether this backend is usable on the current host.

        Returns a list of human-readable problem descriptions; an empty list
        means the backend is ready to use. The default implementation checks
        ``supported_platforms`` and nothing else — backends that rely on an
        external binary (firecracker, cloud-hypervisor, …) should extend it.
        """
        import platform

        if not self.supported_platforms:
            return []
        system = platform.system().lower()
        machine = platform.machine().lower()
        here_short = system
        here_full = f"{system}-{machine}"
        for plat in self.supported_platforms:
            plat_lower = plat.lower()
            if plat_lower == here_short or plat_lower == here_full:
                return []
        return [
            f"host {here_full!r} is not in supported_platforms "
            f"({', '.join(self.supported_platforms)})"
        ]

    def template_artifact_valid(self, template_dir: str) -> bool:
        """Return True if ``template_dir`` contains a complete snapshot for
        this backend. Used by :mod:`flint.core._template_registry` instead of
        hardcoding filenames. The default implementation returns True so that
        backends without a distinct snapshot format don't need to override it.
        """
        return True

    @abstractmethod
    def ensure_runtime_ready(self) -> None:
        pass

    @abstractmethod
    def ensure_default_template(self) -> None:
        pass

    @abstractmethod
    def start_pool(self) -> None:
        pass

    @abstractmethod
    def stop_pool(self) -> None:
        pass

    @abstractmethod
    def create(
        self,
        *,
        template_id: str,
        allow_internet_access: bool,
        use_pool: bool,
        use_pyroute2: bool,
        vm_id: str | None = None,
    ) -> BackendBootResult:
        pass

    @abstractmethod
    def kill(self, entry) -> None:
        pass

    @abstractmethod
    def pause(self, entry, state_store) -> None:
        pass

    @abstractmethod
    def resume(self, row: dict) -> BackendBootResult:
        pass

    def vm_resources(self) -> tuple[int, int]:
        """(vCPUs, memory in MiB) of one VM, for tenant quotas. Firecracker's defaults."""
        return 1, 128

    def discard_paused(self, row: dict) -> None:
        """Delete what a paused sandbox left behind: its snapshot, jail and network."""

    @abstractmethod
    def proxy_guest_request(
        self,
        entry,
        method: str,
        path: str,
        body: bytes | None = None,
        timeout: float = 65,
    ) -> tuple[int, bytes]:
        pass

    @abstractmethod
    async def bridge_terminal(self, entry, websocket) -> None:
        pass

    @abstractmethod
    def check_entry_alive(self, entry) -> tuple[bool, str | None]:
        pass

    def usage(self, entry) -> tuple[float, int] | None:
        """``(cpu_seconds, memory_bytes)`` the VM has used, or None if unknown.

        CPU seconds only grow; the metrics sampler turns them into a percentage.
        The default reads the VMM process on the host (Linux /proc).
        """
        pid = getattr(entry, "pid", 0)
        if not pid:
            return None
        try:
            with open(f"/proc/{pid}/stat") as f:
                parts = f.read().split()
            cpu_seconds = (int(parts[13]) + int(parts[14])) / os.sysconf("SC_CLK_TCK")
            with open(f"/proc/{pid}/status") as f:
                rss = next((int(line.split()[1]) * 1024 for line in f if line.startswith("VmRSS:")), 0)
        except (OSError, IndexError, ValueError):
            return None
        return cpu_seconds, rss

    @abstractmethod
    def recover_row(self, row: dict):
        """Return ``(RecoveryState, entry_or_none)``."""
        pass

    @abstractmethod
    def import_template(self, name: str, image: str, rootfs_size_mb: int = 500, auth: dict | None = None) -> dict:
        """Make template ``name`` from a container image (pulled with ``auth`` if given)."""
        pass

    @abstractmethod
    def delete_template_artifact(self, template_id: str, template: dict | None = None) -> None:
        pass

    # ── Cluster hooks: moving a VM's state between hosts ──────────────────
    #
    # A snapshot is a set of files: name in the snapshot -> path relative to
    # a root directory (the VM's jail). The cluster's snapshot store reads
    # them beneath that root without following symlinks, because the VMM
    # can write inside its jail.

    def pause_files(self, row: dict) -> tuple[str, dict[str, str]]:
        """``(root, files)`` holding a paused sandbox's complete state."""
        raise NotImplementedError(f"backend {self.name!r} can't export snapshots")

    def checkpoint(self, entry, snapshot: Callable[[str, dict[str, str]], Any]) -> Any:
        """Pause a running VM just long enough to call ``snapshot(root, files)``
        with a consistent copy of its state, then resume it. Returns what
        ``snapshot`` returned. The files use :meth:`pause_files` names."""
        raise NotImplementedError(f"backend {self.name!r} can't checkpoint")

    def import_snapshot(self, vm_id: str, src_dir: str, *, allow_internet_access: bool, use_pool: bool = True) -> BackendBootResult:
        """Start ``vm_id`` on this host from a pause or checkpoint snapshot
        restored into ``src_dir`` (files named as in :meth:`pause_files`).
        Consumes ``src_dir``'s files."""
        raise NotImplementedError(f"backend {self.name!r} can't import snapshots")

    def template_files(self, template_dir: str) -> list[str]:
        """Files of a built template, relative to its directory."""
        raise NotImplementedError(f"backend {self.name!r} can't export templates")

    def install_dependencies(self, **kwargs) -> None:  # pragma: no cover - optional
        """Install any external binaries/kernels this backend needs.

        Default: raise, so that ``flint install-deps --backend <name>`` exits
        with a clear error when a plugin hasn't implemented it.
        """
        raise NotImplementedError(
            f"Backend {self.name!r} has no install_dependencies hook"
        )


# Backward-compatible alias — existing code imports ``HostBackend``.
HostBackend = BackendPlugin

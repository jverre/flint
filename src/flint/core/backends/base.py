from __future__ import annotations

import os
import socket
from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any, Callable, ClassVar, Literal

from ..guest import http as guest_http
from ..guest.agent import GuestAgent
from ..types import BootResult

RecoveryState = Literal["alive", "paused", "dead"]


class BackendPlugin(ABC):
    """Contract every Flint backend implements.

    A backend drives one hypervisor: it starts, pauses, resumes and stops VMs
    and finds them again after a restart. It never touches the state store:
    the :class:`~flint.core.manager.SandboxManager` owns sandbox state and
    records every transition.

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

    and declares what it can do with the capability flags below, which the
    manager and daemon check instead of the backend's identity.
    """

    name: ClassVar[str]
    kind: ClassVar[str]
    display_name: ClassVar[str] = ""
    supported_platforms: ClassVar[tuple[str, ...]] = ()

    # ── Capabilities ──────────────────────────────────────────────────────
    # The credential proxy needs the VM's own network namespace.
    supports_network_policy: ClassVar[bool] = True
    # VMs can be allowed to reach their tenant's other VMs (per-VM namespaces on one bridge).
    supports_tenant_networks: ClassVar[bool] = False
    # VMs can boot from a cluster's shared root disks and move between hosts live.
    supports_live_migration: ClassVar[bool] = False
    # Volumes can be mounted into VMs (served over the host bridge network).
    supports_volume_mounts: ClassVar[bool] = False
    # VMMs are confined host processes, so the daemon runs as root and serves
    # its public API from a separate unprivileged process (daemon/supervisor.py).
    supports_confined_api: ClassVar[bool] = False
    # The hypervisor needs the process's main thread (e.g. a macOS RunLoop):
    # the daemon serves its API on another thread and calls run_main_loop().
    needs_main_thread: ClassVar[bool] = False

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

    # ── VM lifecycle ──────────────────────────────────────────────────────

    @abstractmethod
    def create(
        self,
        *,
        template_id: str,
        allow_internet_access: bool,
        use_pool: bool,
        use_pyroute2: bool,
        vm_id: str | None = None,
    ) -> BootResult:
        """Start a new VM from a template."""

    @abstractmethod
    def kill(self, entry) -> None:
        """Stop a VM and delete everything it holds (disk, jail, network)."""

    @abstractmethod
    def pause(self, entry) -> str:
        """Save the running VM's state to disk and stop its VMM.

        Returns where the state is (recorded as the sandbox's pause snapshot).
        On failure the VM keeps running.
        """

    @abstractmethod
    def resume(self, row: Mapping) -> BootResult:
        """Start a paused VM again from the state :meth:`pause` saved."""

    def discard_paused(self, row: Mapping) -> None:
        """Delete what a paused sandbox left behind: its snapshot, jail and network."""

    @abstractmethod
    def check_entry_alive(self, entry) -> tuple[bool, str | None]:
        """Whether a running VM's VMM is still there, and if not, why."""

    @abstractmethod
    def recover_row(self, row: Mapping) -> tuple[RecoveryState, Any]:
        """After a daemon restart: ``("alive", entry)`` for a VM still running
        (adopted), ``("paused", None)``, or ``("dead", None)`` once whatever
        it left is cleaned up."""

    def vm_resources(self) -> tuple[int, int]:
        """(vCPUs, memory in MiB) of one VM, for tenant quotas. Firecracker's defaults."""
        return 1, 128

    # Hooks for host-level confinement (cgroups) around the lifecycle. Backends
    # whose VMMs aren't host processes of their own need none of them.

    def force_stop(self, vm_id: str, *, pid: int, root: str, process=None) -> bool:
        """Stop every process of a VM (before its network is torn down). True if it did anything."""
        return False

    def prepare_recovery(self, rows: list[Mapping]) -> None:
        """Before recovery probes the rows (every active one, maybe none): clear
        what a crashed daemon left behind and get surviving VMMs into a state
        recovery can handle. Raise to refuse to start."""

    def reconcile_recovered(self, row: Mapping) -> None:
        """After recovery, for each running or paused VM: repair host state outside the VMM a restart may have lost."""

    def confine_recovered(self, row: Mapping) -> None:
        """Re-apply resource limits to an adopted VM; raise if it can't be confined."""

    def run_main_loop(self) -> None:
        """Run the hypervisor's main-thread loop, forever (see ``needs_main_thread``)."""
        raise NotImplementedError(f"backend {self.name!r} has no main loop")

    # ── The guest agent ───────────────────────────────────────────────────

    def connect_guest(self, entry) -> socket.socket:
        """Start connecting to the VM's guest agent; see :func:`flint.core.guest.http.connect`."""
        return guest_http.connect(entry.agent_url)

    def guest(self, entry) -> GuestAgent:
        """A client for the VM's guest agent."""
        return GuestAgent(lambda: self.connect_guest(entry))

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

    # ── Templates ─────────────────────────────────────────────────────────

    @abstractmethod
    def build_template(self, name: str, dockerfile: str, rootfs_size_mb: int = 500) -> dict:
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

    def vmm_binary(self) -> str:
        """The hypervisor binary, for host inventory (""if there is none)."""
        return ""

    def pause_files(self, row: Mapping) -> tuple[str, dict[str, str]]:
        """``(root, files)`` holding a paused sandbox's complete state."""
        raise NotImplementedError(f"backend {self.name!r} can't export snapshots")

    def checkpoint(self, entry, snapshot: Callable[[str, dict[str, str]], Any]) -> Any:
        """Pause a running VM just long enough to call ``snapshot(root, files)``
        with a consistent copy of its state, then resume it. Returns what
        ``snapshot`` returned. The files use :meth:`pause_files` names."""
        raise NotImplementedError(f"backend {self.name!r} can't checkpoint")

    def import_snapshot(self, vm_id: str, src_dir: str, *, allow_internet_access: bool, use_pool: bool = True) -> BootResult:
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


__all__ = ["BackendPlugin", "BootResult", "RecoveryState"]

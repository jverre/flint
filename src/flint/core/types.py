from __future__ import annotations

import enum
import json
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .policy import NetworkPolicy, redacted


class SandboxState(enum.Enum):
    STARTING = "Starting"
    RUNNING = "Running"
    PAUSED = "Paused"
    ERROR = "Error"
    DELETING = "Deleting"
    DEAD = "Dead"

    def __str__(self) -> str:
        return self.value


# Backward-compat mapping: API still returns old string names
_STATE_API_NAMES: dict[SandboxState, str] = {
    SandboxState.STARTING: "Starting",
    SandboxState.RUNNING: "Started",
    SandboxState.PAUSED: "Paused",
    SandboxState.ERROR: "Error",
    SandboxState.DELETING: "Deleting",
    SandboxState.DEAD: "Dead",
}


@dataclass
class CommandResult:
    stdout: str
    stderr: str
    exit_code: int


# ── Cluster maintenance state ──────────────────────────────────────────────


class LivePhase(str, enum.Enum):
    """Where a live migration stands, as recorded on each side's sandbox."""

    # Destination
    RECEIVING = "receiving"
    CUTOVER = "cutover"            # the VMM resumed the guest here; irreversible
    RECEIVED = "received"
    CUTOVER_FAILED = "cutover_failed"
    # Source
    SENDING = "sending"
    SENT = "sent"                  # paused here, but its VMM must stay alive until the CP decides
    SEND_FAILED = "send_failed"
    ROLLED_BACK = "rolled_back"


@dataclass
class Maintenance:
    """``backend_metadata["maintenance"]``: a VM's shared root disk, migration
    network and live-migration progress. Serialises to the same JSON as
    always, since the API and the control plane read it."""

    root_disk: dict | None = None
    network: dict | None = None
    live_phase: LivePhase | None = None
    live_executed: bool = False
    live_error: str | None = None
    original_availability: str | None = None

    @classmethod
    def of(cls, metadata: Mapping | None) -> Maintenance:
        raw = (metadata or {}).get("maintenance") or {}
        phase = raw.get("live_phase")
        return cls(
            root_disk=raw.get("root_disk"),
            network=raw.get("network"),
            live_phase=LivePhase(phase) if phase else None,
            live_executed=bool(raw.get("live_executed")),
            live_error=raw.get("live_error"),
            original_availability=raw.get("original_availability"),
        )

    def to_json(self) -> dict:
        out: dict[str, Any] = {}
        for key in ("root_disk", "network"):
            if getattr(self, key) is not None:
                out[key] = getattr(self, key)
        if self.live_phase is not None:
            out["live_phase"] = self.live_phase.value
        if self.live_executed:
            out["live_executed"] = True
        if self.live_error is not None:
            out["live_error"] = self.live_error
        if self.original_availability is not None:
            out["original_availability"] = self.original_availability
        return out

    def save(self, metadata: dict) -> None:
        metadata["maintenance"] = self.to_json()

    @property
    def migratable(self) -> bool:
        """Booted on a shared root disk and migration network, so it can move live."""
        return bool(self.root_disk and self.network)


# ── State-store rows ───────────────────────────────────────────────────────


class SandboxRow(dict):
    """A ``sandboxes`` row from the state store.

    Still a dict (callers index it by column), with typed accessors for what
    the lifecycle code decides on.
    """

    @classmethod
    def of(cls, row: Mapping) -> SandboxRow:
        return row if isinstance(row, SandboxRow) else cls(row)

    @property
    def vm_id(self) -> str:
        return self["vm_id"]

    @property
    def state(self) -> SandboxState | None:
        value = self.get("state")
        return SandboxState(value) if value else None

    @property
    def pid(self) -> int:
        return self.get("pid") or 0

    @property
    def vm_dir(self) -> str:
        return self.get("vm_dir") or ""

    @property
    def ns_name(self) -> str:
        return self.get("ns_name") or ""

    @property
    def socket_path(self) -> str:
        return self.get("socket_path") or ""

    @property
    def chroot_base(self) -> str:
        return self.get("chroot_base") or ""

    @property
    def backend_metadata(self) -> dict:
        """Per-sandbox backend state: a parsed ``backend_metadata`` or the ``backend_meta_json`` column."""
        meta = self.get("backend_metadata")
        if isinstance(meta, dict):
            return meta
        raw = self.get("backend_meta_json")
        if isinstance(raw, str) and raw:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                return {}
            if isinstance(parsed, dict):
                return parsed
        return {}

    @property
    def maintenance(self) -> Maintenance:
        return Maintenance.of(self.backend_metadata)


# ── Booting and tracking VMs ───────────────────────────────────────────────


@dataclass
class BootResult:
    """A VM a backend started (created, resumed or restored). The caller owns it.

    ``ns_name``, ``socket_path`` and ``chroot_base`` are recorded on the
    sandbox's state-store row; anything else a backend needs to find the VM
    again goes in ``metadata``, which the manager stores without reading.
    """

    vm_id: str
    process: Any
    vm_dir: str
    agent_url: str
    guest_ip: str = ""
    ns_name: str = ""
    socket_path: str = ""
    chroot_base: str = ""
    timings: dict[str, float] = field(default_factory=dict)
    t_total: float = 0.0  # time.monotonic() when the boot began
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def pid(self) -> int:
        return getattr(self.process, "pid", 0) or 0


@dataclass
class _SandboxEntry:
    vm_id: str
    process: Any | None
    pid: int
    vm_dir: str
    socket_path: str
    ns_name: str
    guest_ip: str
    agent_url: str
    agent_healthy: bool
    state: SandboxState
    chroot_base: str = ""
    template_id: str = "default"
    backend_kind: str = ""
    backend_metadata: dict[str, Any] = field(default_factory=dict)
    boot_time_ms: float | None = None
    ready_time_ms: float | None = None
    timings: dict[str, float] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    log_lines: deque = field(default_factory=lambda: deque(maxlen=100))
    line_count: int = 0
    network_policy: NetworkPolicy | None = None
    proxy: Any = None  # CredentialProxy instance, if active
    timeout_seconds: float | None = None
    timeout_policy: str | None = None
    tenant: str = ""
    availability: str = ""  # cluster availability class; "" outside a cluster

    @classmethod
    def from_boot(
        cls,
        boot: BootResult,
        *,
        kind: str,
        template_id: str,
        tenant: str = "",
        availability: str = "",
        state: SandboxState = SandboxState.RUNNING,
        network_policy: NetworkPolicy | None = None,
    ) -> _SandboxEntry:
        """A sandbox whose VM a backend just started."""
        return cls(
            vm_id=boot.vm_id,
            process=boot.process,
            pid=boot.pid,
            vm_dir=boot.vm_dir,
            socket_path=boot.socket_path,
            ns_name=boot.ns_name,
            guest_ip=boot.guest_ip,
            agent_url=boot.agent_url,
            agent_healthy=state == SandboxState.RUNNING,
            state=state,
            chroot_base=boot.chroot_base,
            template_id=template_id,
            backend_kind=kind,
            backend_metadata=dict(boot.metadata),
            timings=dict(boot.timings),
            network_policy=network_policy,
            tenant=tenant,
            availability=availability,
        )

    @classmethod
    def from_row(
        cls,
        row: Mapping,
        *,
        kind: str,
        process: Any,
        agent_url: str,
        guest_ip: str,
        state: SandboxState = SandboxState.RUNNING,
        ns_name: str | None = None,
    ) -> _SandboxEntry:
        """A sandbox recovered from its state-store row, its VMM still running."""
        row = SandboxRow.of(row)
        return cls(
            vm_id=row.vm_id,
            process=process,
            pid=row.pid,
            vm_dir=row.vm_dir,
            socket_path=row.socket_path,
            ns_name=row.ns_name if ns_name is None else ns_name,
            guest_ip=guest_ip,
            agent_url=agent_url,
            agent_healthy=True,
            state=state,
            chroot_base=row.chroot_base,
            template_id=row.get("template_id") or "default",
            backend_kind=row.get("backend_kind") or kind,
            backend_metadata=dict(row.backend_metadata),
            tenant=row.get("tenant") or "",
            availability=row.get("availability") or "",
        )

    @property
    def maintenance(self) -> Maintenance:
        return Maintenance.of(self.backend_metadata)

    def to_dict(self) -> dict:
        """Return JSON-serializable representation."""
        return {
            "vm_id": self.vm_id,
            "pid": self.pid,
            "resources": dict(self.backend_metadata.get("resources", {})),
            "maintenance": dict(self.backend_metadata.get("maintenance", {})),
            "state": _STATE_API_NAMES.get(self.state, str(self.state)),
            "template_id": self.template_id,
            "backend_kind": self.backend_kind,
            "agent_healthy": self.agent_healthy,
            "created_at": self.created_at,
            "boot_time_ms": self.boot_time_ms,
            "ready_time_ms": self.ready_time_ms,
            "timings": dict(self.timings),
            "log_lines": list(self.log_lines),
            "line_count": self.line_count,
            "network_policy": redacted(self.network_policy),
            "timeout_seconds": self.timeout_seconds,
            "timeout_policy": self.timeout_policy,
            "tenant": self.tenant,
            "network_address": self.backend_metadata.get("veth_ip", ""),
            "availability": self.availability,
        }

from __future__ import annotations

import enum
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any


class SandboxState(enum.Enum):
    STARTING = "Starting"
    RUNNING = "Running"
    PAUSED = "Paused"
    ERROR = "Error"
    DEAD = "Dead"

    def __str__(self) -> str:
        return self.value


# Backward-compat mapping: API still returns old string names
_STATE_API_NAMES: dict[SandboxState, str] = {
    SandboxState.STARTING: "Starting",
    SandboxState.RUNNING: "Started",
    SandboxState.PAUSED: "Paused",
    SandboxState.ERROR: "Error",
    SandboxState.DEAD: "Dead",
}


@dataclass
class CommandResult:
    stdout: str
    stderr: str
    exit_code: int


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
    backend_kind: str = "linux-firecracker"
    backend_vm_ref: str = ""
    runtime_dir: str = ""
    guest_arch: str = ""
    transport_ref: str = ""
    pause_state_ref: str = ""
    backend_metadata: dict[str, Any] = field(default_factory=dict)
    screen_version: int = 0
    t_instance_start: float = 0.0
    boot_time_ms: float | None = None
    ready_time_ms: float | None = None
    timings: dict[str, float] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    log_lines: deque = field(default_factory=lambda: deque(maxlen=100))
    line_count: int = 0
    network_policy: dict | None = None
    proxy: Any = None  # CredentialProxy instance, if active

    def to_dict(self) -> dict:
        """Return JSON-serializable representation."""
        return {
            "vm_id": self.vm_id,
            "pid": self.pid,
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
            "network_policy": _redact_policy(self.network_policy),
        }


_REDACT_RE = re.compile(r"^(.{4}).+(.{4})$")


def _redact_policy(policy: dict | None) -> dict | None:
    """Return a copy of the network policy with credential values redacted."""
    if policy is None:
        return None
    allow = policy.get("allow")
    if not isinstance(allow, dict):
        return policy
    redacted_allow: dict = {}
    for domain, rules in allow.items():
        redacted_rules = []
        for rule in rules:
            transforms = rule.get("transform")
            if not transforms:
                redacted_rules.append(rule)
                continue
            redacted_transforms = []
            for t in transforms:
                headers = t.get("headers")
                if not headers:
                    redacted_transforms.append(t)
                    continue
                redacted_headers = {}
                for k, v in headers.items():
                    m = _REDACT_RE.match(v)
                    redacted_headers[k] = f"{m.group(1)}***{m.group(2)}" if m else "***"
                redacted_transforms.append({**t, "headers": redacted_headers})
            redacted_rules.append({**rule, "transform": redacted_transforms})
        redacted_allow[domain] = redacted_rules
    return {**policy, "allow": redacted_allow}

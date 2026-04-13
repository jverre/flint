"""Capability protocols for sandbox backends.

Each backend declares what it can do by implementing one or more of these
Protocols. The daemon and SDK use ``isinstance(backend, SupportsX)`` to
dispatch and to gate features.

Why protocols (not mixin ABCs):
- A backend opts in just by implementing the methods - no parallel registry
  to keep in sync.
- Composition without inheritance noise.
- ``isinstance`` checks at boundaries make capability mismatches explicit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


# ── Result / request types ─────────────────────────────────────────────────


@dataclass
class ExecRequest:
    cmd: list[str]
    env: dict[str, str] | None = None
    cwd: str | None = None
    timeout: float = 60


@dataclass
class ExecResult:
    stdout: str
    stderr: str
    exit_code: int


@dataclass
class FileInfo:
    name: str
    path: str
    size: int
    is_dir: bool
    mode: str = ""
    modified_at: float = 0.0


@dataclass
class ProcessSpec:
    cmd: list[str]
    pty: bool = False
    cols: int = 120
    rows: int = 40
    env: dict[str, str] | None = None
    cwd: str | None = None


@dataclass
class ProcessHandle:
    pid: int


@dataclass
class EvalRequest:
    code: str
    timeout: float = 30


@dataclass
class EvalResult:
    result: Any = None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None


@dataclass
class FetchRequest:
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes | None = None


@dataclass
class FetchResponse:
    status: int
    headers: dict[str, str]
    body: bytes


# ── Capability protocols ───────────────────────────────────────────────────


@runtime_checkable
class SupportsShell(Protocol):
    """Run a one-shot command and collect stdout/stderr/exit_code."""

    def exec(self, entry: Any, request: ExecRequest) -> ExecResult: ...


@runtime_checkable
class SupportsFiles(Protocol):
    """Read/write/list files inside the sandbox."""

    def read_file(self, entry: Any, path: str) -> bytes: ...

    def write_file(self, entry: Any, path: str, data: bytes, mode: str = "0644") -> None: ...

    def stat_file(self, entry: Any, path: str) -> FileInfo: ...

    def list_files(self, entry: Any, path: str) -> list[FileInfo]: ...

    def mkdir(self, entry: Any, path: str, parents: bool = True) -> None: ...

    def delete_file(self, entry: Any, path: str, recursive: bool = False) -> None: ...


@runtime_checkable
class SupportsPty(Protocol):
    """Long-lived processes with PTY semantics + interactive terminal bridge."""

    def create_process(self, entry: Any, spec: ProcessSpec) -> ProcessHandle: ...

    def list_processes(self, entry: Any) -> list[dict]: ...

    def send_process_input(self, entry: Any, pid: int, data: bytes) -> None: ...

    def signal_process(self, entry: Any, pid: int, signal: int) -> None: ...

    def resize_process(self, entry: Any, pid: int, cols: int, rows: int) -> None: ...

    async def attach_terminal(self, entry: Any, websocket: Any) -> None: ...


@runtime_checkable
class SupportsPause(Protocol):
    """Snapshot to disk and resume from a stored row."""

    def pause(self, entry: Any, state_store: Any) -> None: ...

    def resume(self, row: dict) -> Any: ...  # BackendBootResult, but kept Any to avoid cycle


@runtime_checkable
class SupportsTemplateBuild(Protocol):
    """Build a custom template artifact for this backend."""

    def build_template(self, name: str, source: Any, **kwargs: Any) -> dict: ...

    def delete_template_artifact(self, template_id: str, template: dict | None = None) -> None: ...


@runtime_checkable
class SupportsPool(Protocol):
    """Maintain a pre-warm pool to lower create() latency."""

    def start_pool(self) -> None: ...

    def stop_pool(self) -> None: ...


# ── JS / Worker capabilities (used by the V8 backend in stage B) ───────────


@runtime_checkable
class SupportsJsEval(Protocol):
    """Evaluate a snippet of JavaScript inside the sandbox."""

    def eval_js(self, entry: Any, request: EvalRequest) -> EvalResult: ...


@runtime_checkable
class SupportsMediatedFetch(Protocol):
    """Issue an HTTP request from inside the sandbox via the credential proxy."""

    def fetch(self, entry: Any, request: FetchRequest) -> FetchResponse: ...


@runtime_checkable
class SupportsKv(Protocol):
    """Per-sandbox key/value store."""

    def kv_get(self, entry: Any, key: str) -> bytes | None: ...

    def kv_put(self, entry: Any, key: str, value: bytes) -> None: ...

    def kv_delete(self, entry: Any, key: str) -> None: ...


# ── Helpers ────────────────────────────────────────────────────────────────


CAPABILITY_PROTOCOLS: dict[str, type] = {
    "shell": SupportsShell,
    "files": SupportsFiles,
    "pty": SupportsPty,
    "pause": SupportsPause,
    "template_build": SupportsTemplateBuild,
    "pool": SupportsPool,
    "js_eval": SupportsJsEval,
    "fetch": SupportsMediatedFetch,
    "kv": SupportsKv,
}


def derive_capabilities(backend: Any) -> frozenset[str]:
    """Return the set of capability names this backend instance satisfies."""
    return frozenset(name for name, proto in CAPABILITY_PROTOCOLS.items() if isinstance(backend, proto))

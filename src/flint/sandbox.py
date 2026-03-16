"""Public Sandbox SDK for Flint — E2B-style interface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from flint._client.client import DaemonClient, _TerminalConnection
from flint.core.config import DAEMON_URL, TERM_COLS, TERM_ROWS

_client: DaemonClient | None = None


def _get_client() -> DaemonClient:
    global _client
    if _client is None:
        _client = DaemonClient()
    return _client


@dataclass
class CommandResult:
    """Result of running a command in a sandbox."""

    stdout: str
    stderr: str
    exit_code: int

    @staticmethod
    def from_response(resp: dict) -> "CommandResult":
        return CommandResult(
            stdout=resp.get("stdout", ""),
            stderr=resp.get("stderr", ""),
            exit_code=resp.get("exit_code", -1),
        )


class PtySession:
    """An interactive PTY session connected to a sandbox."""

    def __init__(self, vm_id: str, conn: _TerminalConnection) -> None:
        self._vm_id = vm_id
        self._conn = conn

    def send_input(self, data: str | bytes) -> None:
        if isinstance(data, str):
            data = data.encode()
        self._conn.send(data)

    def kill(self) -> None:
        self._conn.close()


class Pty:
    """PTY factory for a sandbox."""

    def __init__(self, vm_id: str) -> None:
        self._vm_id = vm_id

    def create(
        self,
        cols: int = TERM_COLS,
        rows: int = TERM_ROWS,
        on_data: Callable[[bytes], None] | None = None,
    ) -> PtySession:
        callback = on_data or (lambda _data: None)
        ws_base = DAEMON_URL.replace("http://", "ws://").replace("https://", "wss://")
        ws_url = f"{ws_base}/vms/{self._vm_id}/terminal"
        conn = _TerminalConnection(ws_url, callback)
        return PtySession(self._vm_id, conn)


class Commands:
    """Run commands in a sandbox and collect output."""

    def __init__(self, vm_id: str) -> None:
        self._vm_id = vm_id

    def run(
        self,
        cmd: str,
        on_stdout: Callable[[str], None] | None = None,
        timeout: float = 60,
    ) -> CommandResult:
        """Execute a shell command via the guest agent. Returns structured result."""
        resp = _get_client().exec_command(self._vm_id, cmd, timeout=timeout)
        result = CommandResult.from_response(resp)
        if on_stdout:
            for line in result.stdout.split("\n"):
                on_stdout(line)
        return result


class Sandbox:
    """A Flint sandbox — wraps a Firecracker microVM.

    Usage::

        from flint import Sandbox

        sandbox = Sandbox()                          # create new VM
        result = sandbox.commands.run("echo hello")  # run a command
        print(result.stdout, result.exit_code)
        sandbox.kill()                               # clean up
    """

    def __init__(
        self,
        vm_id: str | None = None,
        *,
        template_id: str = "default",
        allow_internet_access: bool = True,
        use_pool: bool = True,
        use_pyroute2: bool = True,
    ) -> None:
        client = _get_client()
        if vm_id is None:
            vm = client.create(template_id=template_id, allow_internet_access=allow_internet_access, use_pool=use_pool, use_pyroute2=use_pyroute2)
            self._id = vm["vm_id"]
            self._timings: dict[str, float] = vm.get("timings", {})
            self._ready_time_ms: float | None = vm.get("ready_time_ms")
        else:
            self._id = vm_id
            self._timings = {}
            self._ready_time_ms = None
        self._template_id = template_id
        self._commands = Commands(self._id)
        self._pty = Pty(self._id)

    @property
    def id(self) -> str:
        return self._id

    @property
    def state(self) -> str:
        data = self._fetch()
        return data.get("state", "Unknown") if data else "Unknown"

    @property
    def pid(self) -> int:
        data = self._fetch()
        return data.get("pid", -1) if data else -1

    @property
    def created_at(self) -> float:
        data = self._fetch()
        return data.get("created_at", 0.0) if data else 0.0

    @property
    def timings(self) -> dict[str, float]:
        """Per-step boot timings from the daemon (empty for reconnected VMs)."""
        return self._timings

    @property
    def ready_time_ms(self) -> float | None:
        """Total time-to-ready in ms as measured by the daemon (None for reconnected VMs)."""
        return self._ready_time_ms

    @property
    def commands(self) -> Commands:
        return self._commands

    @property
    def pty(self) -> Pty:
        return self._pty

    def is_running(self) -> bool:
        return self.state in ("Starting", "Started", "Running")

    def kill(self) -> None:
        try:
            _get_client().kill(self._id)
        except Exception:
            pass  # VM may already be deleted

    def pause(self) -> None:
        """Pause the sandbox, preserving state to disk."""
        _get_client().pause(self._id)

    def resume(self) -> None:
        """Resume a paused sandbox."""
        _get_client().resume(self._id)

    def set_timeout(self, timeout_seconds: float, policy: str = "kill") -> None:
        """Set auto-cleanup timeout for this sandbox."""
        _get_client().set_timeout(self._id, timeout_seconds, policy)

    # ── New high-level methods ──────────────────────────────────────────────

    def run_command(self, cmd: str, timeout: float = 60) -> CommandResult:
        """Run a shell command. Returns structured result."""
        return self._commands.run(cmd, timeout=timeout)

    def run_code(self, code: str, runtime: str | None = None, timeout: float = 60) -> CommandResult:
        """Execute code with auto-detected runtime (python/node)."""
        resp = _get_client().run_code(self._id, code, runtime=runtime, timeout=timeout)
        return CommandResult.from_response(resp)

    # ── Filesystem methods ──────────────────────────────────────────────────

    def read_file(self, path: str) -> bytes:
        """Read a file from the sandbox."""
        return _get_client().read_file(self._id, path)

    def write_file(self, path: str, content: bytes | str, mode: str = "0644") -> None:
        """Write a file to the sandbox."""
        if isinstance(content, str):
            content = content.encode()
        _get_client().write_file(self._id, path, content, mode)

    def list_files(self, path: str = "/") -> list[dict]:
        """List files in a directory."""
        return _get_client().list_files(self._id, path)

    def _fetch(self) -> dict | None:
        return _get_client().get(self._id)

    # -- Class / static methods -----------------------------------------------

    @classmethod
    def list(cls) -> list[Sandbox]:
        vms = _get_client().list()
        return [cls.connect(vm["vm_id"]) for vm in vms]

    @classmethod
    def connect(cls, vm_id: str) -> Sandbox:
        """Wrap an existing VM without creating a new one."""
        sb = object.__new__(cls)
        sb._id = vm_id
        sb._timings = {}
        sb._ready_time_ms = None
        sb._commands = Commands(vm_id)
        sb._pty = Pty(vm_id)
        return sb

    @staticmethod
    def is_daemon_running() -> bool:
        return DaemonClient.is_daemon_running()

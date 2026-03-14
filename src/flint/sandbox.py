"""Public Sandbox SDK for Flint — E2B-style interface."""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Callable

from flint._client.client import DaemonClient, _TerminalConnection
from flint.core.config import DAEMON_URL, TERM_COLS, TERM_ROWS

_client: DaemonClient | None = None
_SENTINEL = re.compile(r"__FLINT_DONE__:(\d+)\r?\n?$")


def _get_client() -> DaemonClient:
    global _client
    if _client is None:
        _client = DaemonClient()
    return _client


@dataclass
class CommandResult:
    """Result of running a command in a sandbox."""

    stdout: str
    exit_code: int


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
        collected: list[bytes] = []
        done = threading.Event()
        result_holder: list[CommandResult] = []

        def _on_output(data: bytes) -> None:
            collected.append(data)
            text = b"".join(collected).decode(errors="replace")
            m = _SENTINEL.search(text)
            if m:
                exit_code = int(m.group(1))
                stdout = text[: m.start()]
                # Strip the command echo (first line) and trailing newline
                lines = stdout.split("\n")
                if lines and lines[0].rstrip().endswith(f'echo "__FLINT_DONE__:$?"'):
                    lines = lines[1:]
                stdout = "\n".join(lines).strip()
                if on_stdout:
                    for line in stdout.split("\n"):
                        on_stdout(line)
                result_holder.append(CommandResult(stdout=stdout, exit_code=exit_code))
                done.set()

        ws_base = DAEMON_URL.replace("http://", "ws://").replace("https://", "wss://")
        ws_url = f"{ws_base}/vms/{self._vm_id}/terminal"
        conn = _TerminalConnection(ws_url, _on_output)
        try:
            sentinel_cmd = f'{cmd} ; echo "__FLINT_DONE__:$?"\n'
            conn.send(sentinel_cmd.encode())
            if not done.wait(timeout=timeout):
                raise TimeoutError(f"Command timed out after {timeout}s: {cmd}")
            return result_holder[0]
        finally:
            conn.close()


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
        return self.state in ("Starting", "Started")

    def kill(self) -> None:
        _get_client().kill(self._id)

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

from __future__ import annotations

from typing import Callable, TYPE_CHECKING

from .types import _SandboxEntry
from .commands import Commands
from .files import Files
from .pty import Pty

if TYPE_CHECKING:
    from .manager import SandboxManager


class Sandbox:
    """Public facade for a single sandbox."""

    def __init__(self, entry: _SandboxEntry, manager: SandboxManager) -> None:
        self._entry = entry
        self._manager = manager
        self.commands = Commands(self)
        self.files = Files(self.commands)
        self.pty = Pty(self)

    @property
    def id(self) -> str:
        return self._entry.vm_id

    @property
    def state(self) -> str:
        return self._entry.state

    @property
    def pid(self) -> int:
        return self._entry.pid

    @property
    def tcp_connected(self) -> bool:
        return self._entry.tcp_connected

    @property
    def created_at(self) -> float:
        return self._entry.created_at

    @property
    def boot_time_ms(self) -> float | None:
        return self._entry.boot_time_ms

    @property
    def ready_time_ms(self) -> float | None:
        return self._entry.ready_time_ms

    @property
    def timings(self) -> dict[str, float]:
        return self._entry.timings

    @property
    def log_lines(self):
        return self._entry.log_lines

    @property
    def line_count(self) -> int:
        return self._entry.line_count

    def send_raw(self, data: str | bytes) -> None:
        """Send raw bytes to the sandbox's TCP socket."""
        self._entry.send_raw(data)

    def subscribe_output(self, cb: Callable[[bytes], None]) -> None:
        self._entry.subscribe_output(cb)

    def unsubscribe_output(self, cb: Callable[[bytes], None]) -> None:
        self._entry.unsubscribe_output(cb)

    def kill(self) -> None:
        self._manager.kill(self._entry.vm_id)

from __future__ import annotations

from typing import Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from .sandbox import Sandbox


class PtySession:
    """An interactive terminal session connected to a sandbox."""

    def __init__(self, sandbox: Sandbox, cols: int, rows: int) -> None:
        self._sandbox = sandbox
        self._cols = cols
        self._rows = rows
        self._data_callbacks: list[Callable[[bytes], None]] = []
        sandbox.subscribe_output(self._on_data)

    def send_input(self, data: str | bytes) -> None:
        """Send raw input (keystrokes, commands) to the terminal."""
        self._sandbox.send_raw(data)

    def on_data(self, callback: Callable[[bytes], None]) -> None:
        """Register callback for output data from the terminal."""
        self._data_callbacks.append(callback)

    def resize(self, cols: int, rows: int) -> None:
        """Resize the terminal. Sends SIGWINCH via stty."""
        self._cols, self._rows = cols, rows
        self._sandbox.send_raw(f"stty cols {cols} rows {rows}\n".encode())

    def kill(self) -> None:
        """Disconnect this PTY session (does not kill the sandbox)."""
        self._sandbox.unsubscribe_output(self._on_data)
        self._data_callbacks.clear()

    def _on_data(self, data: bytes) -> None:
        for cb in self._data_callbacks:
            cb(data)


class Pty:
    """Factory for PTY sessions on a sandbox. Accessed as sandbox.pty."""

    def __init__(self, sandbox: Sandbox) -> None:
        self._sandbox = sandbox

    def create(self, cols: int = 120, rows: int = 40) -> PtySession:
        return PtySession(self._sandbox, cols, rows)

"""Typed Textual messages broadcast by the app when shared state changes.

`FlintApp.apply_event` is the only mutator of `AppState`. After each mutation
it posts the relevant message to the current screen so widgets can react.
"""

from __future__ import annotations

from textual.message import Message


class SandboxesChanged(Message):
    """Posted when the sandbox dict was mutated (created/updated/deleted)."""

    def __init__(self, reason: str = "") -> None:
        super().__init__()
        self.reason = reason


class VolumesChanged(Message):
    def __init__(self, reason: str = "") -> None:
        super().__init__()
        self.reason = reason


class WsStateChanged(Message):
    def __init__(self, connected: bool) -> None:
        super().__init__()
        self.connected = connected

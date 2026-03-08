from __future__ import annotations

import re
import threading
import uuid
from typing import TYPE_CHECKING

from .types import CommandResult

if TYPE_CHECKING:
    from .sandbox import Sandbox

_EXIT_MARKER_RE = re.compile(r"FLINT_EXIT:(\d+)")


class Commands:
    """Structured command execution on a sandbox. Accessed as sandbox.commands."""

    def __init__(self, sandbox: Sandbox) -> None:
        self._sandbox = sandbox

    def run(self, cmd: str, timeout: float = 30.0) -> CommandResult:
        """Execute a command and wait for structured output.

        Sends the command with an exit marker, collects output until the marker
        is detected, and returns a CommandResult. stderr is mixed into stdout
        (limitation of the raw TCP transport).
        """
        marker_id = uuid.uuid4().hex[:8]
        marker = f"FLINT_EXIT_{marker_id}"
        wrapped = f"{cmd} ; echo \"{marker}:$?\""

        output_chunks: list[bytes] = []
        done = threading.Event()
        exit_code_holder: list[int] = []

        marker_pattern = re.compile(re.escape(marker) + r":(\d+)")

        def on_data(data: bytes):
            output_chunks.append(data)
            combined = b"".join(output_chunks).decode(errors="replace")
            m = marker_pattern.search(combined)
            if m:
                exit_code_holder.append(int(m.group(1)))
                done.set()

        self._sandbox.subscribe_output(on_data)
        try:
            self._sandbox.send_raw(f"{wrapped}\n")
            done.wait(timeout=timeout)
        finally:
            self._sandbox.unsubscribe_output(on_data)

        combined = b"".join(output_chunks).decode(errors="replace")

        # Strip the marker line and everything after it
        m = marker_pattern.search(combined)
        if m:
            stdout = combined[:m.start()].rstrip("\n")
            exit_code = int(m.group(1))
        else:
            stdout = combined.rstrip("\n")
            exit_code = -1

        # Strip the echoed command from the beginning if present
        lines = stdout.split("\n")
        if lines and wrapped in lines[0]:
            lines = lines[1:]
        stdout = "\n".join(lines)

        return CommandResult(stdout=stdout, stderr="", exit_code=exit_code)

    def send(self, cmd: str) -> None:
        """Fire-and-forget command execution."""
        self._sandbox.send_raw(f"{cmd}\n")

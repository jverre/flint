from __future__ import annotations

import base64
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .commands import Commands


class Files:
    """File operations on a sandbox. Accessed as sandbox.files."""

    def __init__(self, commands: Commands) -> None:
        self._commands = commands

    def read(self, path: str) -> str:
        """Read file contents from the sandbox."""
        result = self._commands.run(f"cat {path}")
        if result.exit_code != 0:
            raise FileNotFoundError(f"Failed to read {path}: exit code {result.exit_code}")
        return result.stdout

    def write(self, path: str, content: str | bytes) -> None:
        """Write content to a file in the sandbox."""
        if isinstance(content, str):
            content = content.encode()
        encoded = base64.b64encode(content).decode()
        result = self._commands.run(f"echo '{encoded}' | base64 -d > {path}")
        if result.exit_code != 0:
            raise IOError(f"Failed to write {path}: exit code {result.exit_code}")

    def list(self, path: str = "/") -> list[str]:
        """List directory contents in the sandbox."""
        result = self._commands.run(f"ls -1 {path}")
        if result.exit_code != 0:
            raise FileNotFoundError(f"Failed to list {path}: exit code {result.exit_code}")
        return [line for line in result.stdout.split("\n") if line]

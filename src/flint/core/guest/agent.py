"""A typed client for the guest agent (flintd) inside every sandbox.

It speaks flintd's HTTP API over connections from a ``connect`` callable, so
it works the same whoever opens the connection: the backend (in the
privileged runtime, entering the VM's network namespace) or the runtime RPC
(in the unprivileged API, which receives the connected socket as an fd).

Blocking methods are for the runtime's own small requests (warm-up, CA
install, volume mounts); ``async`` ones are for the API serving clients.
Every request is bounded as :mod:`.http` describes.
"""

from __future__ import annotations

import asyncio
import base64
import json
import socket
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import cached_property

from . import http
from .http import GuestAgentError

Connect = Callable[[], socket.socket]


@dataclass(frozen=True)
class ExecResult:
    """The agent's reply to ``POST /exec``."""

    status: int
    body: bytes

    @cached_property
    def json(self) -> dict:
        if self.status != 200:
            return {}
        try:
            value = json.loads(self.body)
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}

    @property
    def exit_code(self) -> int | None:
        return self.json.get("exit_code")

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    @property
    def error(self) -> str:
        """What went wrong, for a message: the command's stderr/stdout, else the agent's reply."""
        return str(self.json.get("stderr") or self.json.get("stdout") or self.body.decode(errors="replace")).strip()


class GuestAgent:
    def __init__(self, connect: Connect) -> None:
        self._connect = connect

    # ── Blocking ────────────────────────────────────────────────────────────

    def request(self, method: str, path: str, body: bytes | None = None, timeout: float = 65) -> tuple[int, bytes]:
        return http.request(self._connect(), method, path, body, timeout)

    def exec(self, cmd: list[str], *, timeout: float = 30, request_timeout: float | None = None) -> ExecResult:
        """Run ``cmd`` to completion; the agent kills it after ``timeout`` seconds."""
        body = json.dumps({"cmd": cmd, "timeout": timeout}).encode()
        return ExecResult(*self.request("POST", "/exec", body, request_timeout or timeout + 10))

    def sh(self, script: str, *args: str, timeout: float = 30, request_timeout: float | None = None) -> ExecResult:
        """Run a shell script with positional arguments (``$1``…), so values are never interpolated."""
        return self.exec(["/bin/sh", "-c", script, "sh", *args], timeout=timeout, request_timeout=request_timeout)

    def post_json(self, path: str, body: dict, timeout: float = 65) -> tuple[int, bytes]:
        return self.request("POST", path, json.dumps(body).encode(), timeout)

    # ── Async ───────────────────────────────────────────────────────────────

    async def _socket(self) -> socket.socket:
        # Connecting may cross the runtime RPC: keep it off the event loop.
        return await asyncio.to_thread(self._connect)

    async def call(self, method: str, path: str, body: bytes | None = None, timeout: float = 65) -> tuple[int, bytes]:
        return await http.call(await self._socket(), method, path, body, timeout)

    async def start_process(self, cmd: list[str], *, pty: bool = False, cols: int = 0, rows: int = 0) -> int | None:
        """Start a background process; its guest pid, or None if the agent refused."""
        spec: dict = {"cmd": cmd}
        if pty:
            spec.update(pty=True, cols=cols, rows=rows)
        status, body = await self.call("POST", "/processes", json.dumps(spec).encode())
        if status != 201:
            return None
        return json.loads(body)["pid"]

    async def send_input(self, pid: int, data: bytes, timeout: float = 5) -> None:
        await self.call("POST", f"/processes/{pid}/input", data, timeout)

    async def signal(self, pid: int, signal: int, timeout: float = 5) -> None:
        await self.call("POST", f"/processes/{pid}/signal", json.dumps({"signal": signal}).encode(), timeout)

    @asynccontextmanager
    async def process_output(self, pid: int) -> AsyncIterator[AsyncIterator[bytes]]:
        """The process's stdout/stderr, decoded, until it exits (``/processes/{pid}/output``)."""
        from websockets.asyncio.client import connect

        sock = await self._socket()
        try:
            await http.wait_connected(sock)
            async with connect(f"ws://guest/processes/{pid}/output", sock=sock, proxy=None) as guest:
                async def chunks() -> AsyncIterator[bytes]:
                    async for message in guest:
                        event = json.loads(message)
                        if event.get("type") == "exit":
                            return
                        if event.get("type") in ("stdout", "stderr") and event.get("data"):
                            yield base64.b64decode(event["data"])
                yield chunks()
        finally:
            sock.close()


__all__ = ["Connect", "ExecResult", "GuestAgent", "GuestAgentError"]

"""Per-VM log capture + pub/sub.

`LogBus.append(vm_id, line)` is safe to call from any thread; subscribers
receive the line via an asyncio.Queue inside the daemon's event loop. A short
ring buffer (backed by the `log_lines` deque on `_SandboxEntry`) is also
appended to so late subscribers get recent history.

Populating lines from backend-specific serial/console capture is a follow-up —
for now the daemon itself publishes lifecycle lines so the pane has content.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque

from .config import log as _log


class LogBus:
    def __init__(self, loop: asyncio.AbstractEventLoop, maxsize: int = 512) -> None:
        self._loop = loop
        self._maxsize = maxsize
        self._subs: dict[str, set[asyncio.Queue]] = {}
        self._buffers: dict[str, deque] = {}

    def history(self, vm_id: str) -> list[str]:
        buf = self._buffers.get(vm_id)
        return list(buf) if buf else []

    def subscribe(self, vm_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._maxsize)
        self._subs.setdefault(vm_id, set()).add(q)
        return q

    def unsubscribe(self, vm_id: str, q: asyncio.Queue) -> None:
        subs = self._subs.get(vm_id)
        if subs:
            subs.discard(q)
            if not subs:
                self._subs.pop(vm_id, None)

    def append(self, vm_id: str, line: str) -> None:
        """Append a log line. Safe from any thread."""
        try:
            self._loop.call_soon_threadsafe(self._deliver, vm_id, line)
        except RuntimeError:
            pass

    def _deliver(self, vm_id: str, line: str) -> None:
        buf = self._buffers.setdefault(vm_id, deque(maxlen=1000))
        buf.append(line)
        for q in list(self._subs.get(vm_id, ())):
            try:
                q.put_nowait(line)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(line)
                except asyncio.QueueFull:
                    _log.warning("LogBus: subscriber queue still full after drop-oldest")

    def drop_vm(self, vm_id: str) -> None:
        self._buffers.pop(vm_id, None)
        self._subs.pop(vm_id, None)


_bus: LogBus | None = None


def init_bus(loop: asyncio.AbstractEventLoop) -> LogBus:
    global _bus
    _bus = LogBus(loop)
    return _bus


def get_bus() -> LogBus | None:
    return _bus


def append(vm_id: str, line: str) -> None:
    bus = _bus
    if bus is not None:
        ts = time.strftime("%H:%M:%S")
        bus.append(vm_id, f"[{ts}] {line}")

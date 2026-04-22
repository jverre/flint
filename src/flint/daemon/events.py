"""Fire-and-forget pub/sub for daemon-side state changes.

`publish(...)` is safe to call from any thread. Each WebSocket subscriber gets
a bounded asyncio.Queue; on overflow the oldest event is dropped.

Event types (payload keys other than `type` and `ts`):
  vm.created           vm: dict
  vm.deleted           vm_id: str
  vm.state_changed     vm_id: str, from: str, to: str
  vm.paused            vm_id: str
  vm.resumed           vm: dict
  volume.created       volume: dict
  volume.deleted       volume_id: str
  volume.attached      vm_id: str, volume_id: str, mount_path: str
  volume.detached      vm_id: str, volume_id: str
"""

from __future__ import annotations

import asyncio
import time

from flint.core.config import log


class EventBus:
    def __init__(self, loop: asyncio.AbstractEventLoop, maxsize: int = 256) -> None:
        self._loop = loop
        self._maxsize = maxsize
        self._queues: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._maxsize)
        self._queues.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._queues.discard(q)

    def publish(self, event_type: str, **payload) -> None:
        event = {"type": event_type, "ts": time.time(), **payload}
        try:
            self._loop.call_soon_threadsafe(self._fanout, event)
        except RuntimeError:
            # Loop already closed; drop the event.
            pass

    def _fanout(self, event: dict) -> None:
        for q in list(self._queues):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    log.warning("EventBus: subscriber queue still full after drop-oldest; losing event")


_bus: EventBus | None = None


def init_bus(loop: asyncio.AbstractEventLoop) -> EventBus:
    global _bus
    _bus = EventBus(loop)
    return _bus


def get_bus() -> EventBus | None:
    return _bus


def publish(event_type: str, **payload) -> None:
    """Publish to the global bus. No-op if the bus is not yet initialized."""
    bus = _bus
    if bus is not None:
        bus.publish(event_type, **payload)

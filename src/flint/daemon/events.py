"""Fire-and-forget pub/sub for daemon-side state changes.

`publish(...)` is safe to call from any thread. Each WebSocket subscriber gets
a bounded asyncio.Queue; on overflow the oldest event is dropped.

Every event carries the `tenant` it belongs to; `/events` subscribers only
get their tenants' events.

Event types (payload keys other than `type`, `ts` and `tenant`):
  vm.created           vm: dict
  vm.deleted           vm_id: str
  vm.state_changed     vm_id: str, from: str, to: str
  vm.paused            vm_id: str
  vm.resumed           vm: dict
  vm.wiped             vm_id: str, files: int, bytes: int  (its files were wiped before removal)
  volume.created       volume: dict
  volume.deleted       volume_id: str
  volume.attached      vm_id: str, volume_id: str, mount_path: str
  volume.detached      vm_id: str, volume_id: str, mount_path: str
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


# Called with every published event (from any thread); the cluster agent
# uses this to forward events to the control plane.
hooks: list = []

# vm_id -> its tenant ("" if unknown); set by the daemon.
vm_tenant = lambda _vm_id: ""  # noqa: E731


def _tenant_of(payload: dict) -> str:
    vm = payload.get("vm")
    if isinstance(vm, dict) and vm.get("tenant"):
        return vm["tenant"]
    volume = payload.get("volume")
    if isinstance(volume, dict) and volume.get("tenant"):
        return volume["tenant"]
    vm_id = payload.get("vm_id") or (vm or {}).get("vm_id")
    if vm_id:
        try:
            return vm_tenant(vm_id) or ""
        except Exception:
            log.exception("event tenant lookup failed")
    return ""


def publish(event_type: str, **payload) -> None:
    """Publish to the global bus. No-op if the bus is not yet initialized."""
    if not payload.get("tenant"):
        payload["tenant"] = _tenant_of(payload)
    for hook in hooks:
        try:
            hook({"type": event_type, "ts": time.time(), **payload})
        except Exception:
            log.exception("event hook failed")
    bus = _bus
    if bus is not None:
        bus.publish(event_type, **payload)

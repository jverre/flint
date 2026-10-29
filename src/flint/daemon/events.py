"""Fire-and-forget pub/sub for daemon-side state changes.

`publish(...)` is safe to call from any thread. Each WebSocket subscriber gets
a bounded asyncio.Queue; on overflow the oldest event is dropped.

Every event has its `type`, `ts`, the `tenant` it belongs to, the `actor`
that caused it (and `actor_name`; see eventlog.py) and, once stored, its
`seq`; `/events` subscribers only get their tenants' events. Every event
about a VM has its `vm_id`, and every event about a volume its `volume_id`.

Event types and the rest of their payload:
  vm.created           vm: dict (the VM as GET /vms/{id} shows it)
  vm.deleted           -
  vm.state_changed     from: str, to: str
  vm.paused            -
  vm.resumed           vm: dict
  vm.checkpoint        snapshot_id: str, timings: dict  (a cluster snapshotted the running VM)
  vm.wiped             files: int, bytes: int  (its files were wiped before removal)
  vm.exec              cmd: str, exit_code: int | None, duration_ms: float, stdout_tail: str,
                       stderr_tail: str, error: str (if the guest refused it)
  vm.process_started   cmd: str, pid: int | None
  volume.created       volume: dict
  volume.deleted       -
  volume.attached      vm_id: str, mount_path: str
  volume.detached      vm_id: str, mount_path: str

A cluster's control plane adds its own (vm.moved, vm.checkpointed, host.*).
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

    def publish(self, event: dict) -> None:
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

# Stores an event and returns its seq; set by the daemon (eventlog.EventLog.record).
sink = None


def _tenant_of(payload: dict) -> str:
    vm = payload.get("vm")
    if isinstance(vm, dict) and vm.get("tenant"):
        return vm["tenant"]
    volume = payload.get("volume")
    if isinstance(volume, dict) and volume.get("tenant"):
        return volume["tenant"]
    vm_id = payload.get("vm_id")
    if vm_id:
        try:
            return vm_tenant(vm_id) or ""
        except Exception:
            log.exception("event tenant lookup failed")
    return ""


def publish(event_type: str, **payload) -> None:
    """Store an event and publish it to the global bus (if it is initialized yet)."""
    for key, field in (("vm", "vm_id"), ("volume", "volume_id")):
        resource = payload.get(key)
        if isinstance(resource, dict) and not payload.get(field):
            payload[field] = resource.get(field) or resource.get("id", "")
    if not payload.get("tenant"):
        payload["tenant"] = _tenant_of(payload)
    if not payload.get("actor"):
        from flint.daemon.eventlog import actor
        payload["actor"], name = actor.get()
        if name:
            payload["actor_name"] = name
    event = {"type": event_type, "ts": time.time(), **payload}
    if sink is not None:
        try:
            event["seq"] = sink(event)
        except Exception:
            log.exception("storing event %s failed", event_type)
    for hook in hooks:
        try:
            hook(event)
        except Exception:
            log.exception("event hook failed")
    bus = _bus
    if bus is not None:
        bus.publish(event)

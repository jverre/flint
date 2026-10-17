"""Pre-built network namespaces, so create() skips netns + TAP + veth + firewall setup.

Isolation rules this pool keeps:

- A namespace is built from scratch for exactly one VM and deleted with it. It
  is never returned to the pool or reused, so no ARP, conntrack or iptables
  state carries over between sandboxes.
- Pooled namespaces go through the same setup as on-demand ones, including the
  bridge port isolation and ingress firewall, so an idle namespace on the
  bridge is unreachable from other sandboxes.
- Nothing tenant-specific (credential proxy, network policy) is applied until
  after a VM claims the namespace.
- Unclaimed namespaces are deleted on shutdown and, after a crash, at the next
  start (tracked with marker files).
"""

from __future__ import annotations

import os
import threading
import uuid
from dataclasses import dataclass
from typing import Callable

from . import config
from .config import log
from ._netns import _delete_netns, _setup_netns_pyroute2, _setup_netns_subprocess

# Namespaces kept ready for sandboxes without internet access (rarer).
OFFLINE_POOL_SIZE = 2


@dataclass
class NetSlot:
    ns_name: str
    tap_name: str
    veth_ip: str       # "" without internet access
    internet: bool


_lock = threading.Lock()
_ready: dict[bool, list[NetSlot]] = {True: [], False: []}
_building: dict[bool, int] = {True: 0, False: 0}
_stop = threading.Event()
_threads: list[threading.Thread] = []
_naming: tuple[str, Callable[[str], str]] | None = None  # (ns prefix, ns_name -> TAP name)


def _marker_dir() -> str:
    return os.path.join(config.STATE_DIR, "netns-pool")


def _set_marker(ns_name: str, present: bool) -> None:
    path = os.path.join(_marker_dir(), ns_name)
    if present:
        open(path, "w").close()
    else:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def build_netns(
    prefix: str,
    tap_name_for: Callable[[str], str],
    internet: bool,
    *,
    use_pyroute2: bool = True,
    pooled: bool = False,
) -> NetSlot:
    """Create a fresh namespace for one VM. Caller owns it (``_delete_netns`` on teardown)."""
    ns_name = f"{prefix}{uuid.uuid4().hex[:8]}"
    tap_name = tap_name_for(ns_name)
    if pooled:
        # Before creating it, so a crash mid-build still gets cleaned up.
        _set_marker(ns_name, True)
    try:
        setup = _setup_netns_pyroute2 if use_pyroute2 else _setup_netns_subprocess
        veth_ip = setup(ns_name, tap_name, internet=internet)
    except Exception:
        _delete_netns(ns_name)
        _set_marker(ns_name, False)
        raise
    return NetSlot(ns_name, tap_name, veth_ip, internet)


def claim_netns(internet: bool) -> NetSlot | None:
    """Take a pre-built namespace, or None if the pool is empty."""
    while True:
        with _lock:
            slot = _ready[internet].pop() if _ready[internet] else None
        if slot is None:
            return None
        _set_marker(slot.ns_name, False)
        if os.path.exists(f"/var/run/netns/{slot.ns_name}"):
            return slot
        # Removed behind our back (e.g. manual cleanup); release its IP and skip it.
        log.warning("netns pool: %s vanished before use, skipping", slot.ns_name)
        _delete_netns(slot.ns_name)


def acquire_netns(
    prefix: str,
    tap_name_for: Callable[[str], str],
    internet: bool,
    *,
    use_pool: bool = True,
    use_pyroute2: bool = True,
) -> NetSlot:
    """A namespace for a new VM: from the pool when possible, else built now."""
    slot = claim_netns(internet) if use_pool else None
    return slot or build_netns(prefix, tap_name_for, internet, use_pyroute2=use_pyroute2)


def _target(internet: bool) -> int:
    return config.NETNS_POOL_SIZE if internet else OFFLINE_POOL_SIZE


def _refill_loop() -> None:
    while not _stop.is_set():
        kind = None
        with _lock:
            for internet in (True, False):
                if len(_ready[internet]) + _building[internet] < _target(internet):
                    kind = internet
                    _building[internet] += 1
                    break
        if kind is None:
            _stop.wait(0.05)
            continue
        try:
            slot = build_netns(*_naming, kind, pooled=True)
        except Exception:
            log.exception("netns pool: build failed")
            _stop.wait(1.0)
            slot = None
        finally:
            with _lock:
                _building[kind] -= 1
        if slot:
            with _lock:
                if not _stop.is_set():
                    _ready[kind].append(slot)
                    slot = None
            if slot:  # stopped while building
                _discard(slot)


def _discard(slot: NetSlot) -> None:
    _delete_netns(slot.ns_name)
    _set_marker(slot.ns_name, False)


def _delete_orphans() -> None:
    """Remove namespaces a previous daemon pre-built but never handed out."""
    for ns_name in os.listdir(_marker_dir()):
        _delete_netns(ns_name)
        _set_marker(ns_name, False)


def start_netns_pool(prefix: str, tap_name_for: Callable[[str], str], workers: int = 2) -> None:
    global _naming, _threads
    _naming = (prefix, tap_name_for)
    os.makedirs(_marker_dir(), exist_ok=True)
    _delete_orphans()
    _stop.clear()
    _threads = [
        threading.Thread(target=_refill_loop, daemon=True, name=f"netns-pool-{i}")
        for i in range(workers)
    ]
    for t in _threads:
        t.start()
    log.debug("netns pool: started %d workers (target=%d)", workers, config.NETNS_POOL_SIZE)


def stop_netns_pool() -> None:
    global _threads
    _stop.set()
    for t in _threads:
        t.join(timeout=10.0)
    _threads = []
    with _lock:
        slots = _ready[True] + _ready[False]
        _ready[True], _ready[False] = [], []
    for slot in slots:
        _discard(slot)

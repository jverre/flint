"""Per-VM metrics sampler.

Asks the backend for each running VM's usage at 1 Hz and keeps a bounded
ring buffer per VM. Linux backends report the VMM process (CPU time, RSS);
macOS reports the guest (vCPU busy time, memory in use), since all its VMs
run inside the daemon's process.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Callable

from .config import log


class MetricsSampler:
    def __init__(
        self,
        get_vms: Callable,
        usage: Callable[[object], tuple[float, int] | None],
        *,
        interval: float = 1.0,
        window: int = 60,
    ) -> None:
        self._get_vms = get_vms
        self._usage = usage
        self._interval = interval
        self._window = window
        self._buffers: dict[str, deque] = {}
        self._prev: dict[str, dict] = {}
        self._task: asyncio.Task | None = None

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._task is None:
            self._task = loop.create_task(self._run())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                # In a thread: a backend may ask the guest over the network.
                await asyncio.to_thread(self.sample)
            except Exception:
                log.exception("MetricsSampler tick failed")
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                return

    def sample(self) -> None:
        entries = self._get_vms()
        active_ids = set()
        for entry in entries:
            active_ids.add(entry.vm_id)
            sample = self._sample_one(entry)
            if sample is None:
                continue
            buf = self._buffers.setdefault(entry.vm_id, deque(maxlen=self._window))
            buf.append(sample)
        for vid in list(self._buffers):
            if vid not in active_ids:
                self._buffers.pop(vid, None)
                self._prev.pop(vid, None)

    def _sample_one(self, entry) -> dict | None:
        ts = time.time()
        try:
            usage = self._usage(entry)
        except Exception:
            usage = None
        # Unknown (process gone, guest busy): a zero sample, so the UI still has a point.
        cpu_seconds, mem_bytes = usage or (None, 0)

        cpu_percent = 0.0
        prev = self._prev.get(entry.vm_id)
        if cpu_seconds is not None and prev and prev["cpu_seconds"] is not None:
            dt = ts - prev["ts"]
            used = cpu_seconds - prev["cpu_seconds"]
            if dt > 0 and used >= 0:
                cpu_percent = used / dt * 100
        self._prev[entry.vm_id] = {"ts": ts, "cpu_seconds": cpu_seconds}

        return {
            "ts": ts,
            "cpu_percent": round(cpu_percent, 2),
            "rss_bytes": mem_bytes,
        }

    def get(self, vm_id: str, window: int | None = None) -> list[dict]:
        buf = self._buffers.get(vm_id)
        if not buf:
            return []
        samples = list(buf)
        if window is not None and len(samples) > window:
            samples = samples[-window:]
        return samples

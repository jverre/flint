"""Per-VM metrics sampler.

Polls /proc/<vmm_pid>/{stat,status} at 1 Hz for each known VM and keeps a
bounded ring buffer per VM. Linux-only path for now (CPU ticks, RSS). On
macOS the sampler still runs but produces zero samples — richer data can be
added via host-provided VMM stats later.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections import deque
from typing import Callable

from .config import log


class MetricsSampler:
    def __init__(
        self,
        get_vms: Callable,
        *,
        interval: float = 1.0,
        window: int = 60,
    ) -> None:
        self._get_vms = get_vms
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
                self._sample_all()
            except Exception:
                log.exception("MetricsSampler tick failed")
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                return

    def _sample_all(self) -> None:
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
        pid = getattr(entry, "pid", 0)
        if not pid:
            return None
        ts = time.time()
        cpu_ticks = 0
        rss_bytes = 0
        try:
            with open(f"/proc/{pid}/stat") as f:
                parts = f.read().split()
            cpu_ticks = int(parts[13]) + int(parts[14])
            with open(f"/proc/{pid}/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        rss_bytes = int(line.split()[1]) * 1024
                        break
        except (OSError, IndexError, ValueError):
            # Non-Linux, or process gone — return a zero sample so the
            # UI still has a data point.
            pass

        cpu_percent = 0.0
        prev = self._prev.get(entry.vm_id)
        if prev:
            dt = ts - prev["ts"]
            dticks = cpu_ticks - prev["cpu_ticks"]
            try:
                hz = os.sysconf("SC_CLK_TCK")
            except (AttributeError, ValueError, OSError):
                hz = 100
            if dt > 0 and dticks >= 0:
                cpu_percent = (dticks / hz / dt) * 100
        self._prev[entry.vm_id] = {"ts": ts, "cpu_ticks": cpu_ticks}

        return {
            "ts": ts,
            "cpu_percent": round(cpu_percent, 2),
            "rss_bytes": rss_bytes,
        }

    def get(self, vm_id: str, window: int | None = None) -> list[dict]:
        buf = self._buffers.get(vm_id)
        if not buf:
            return []
        samples = list(buf)
        if window is not None and len(samples) > window:
            samples = samples[-window:]
        return samples

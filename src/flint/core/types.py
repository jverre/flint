from __future__ import annotations

import subprocess
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class CommandResult:
    stdout: str
    stderr: str
    exit_code: int


@dataclass
class _SandboxEntry:
    vm_id: str
    process: subprocess.Popen | None
    pid: int
    vm_dir: str
    socket_path: str
    ns_name: str
    guest_ip: str
    tcp_socket: socket.socket | None
    tcp_connected: bool
    state: str
    screen_version: int = 0
    t_instance_start: float = 0.0
    boot_time_ms: float | None = None
    ready_time_ms: float | None = None
    timings: dict[str, float] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    log_lines: deque = field(default_factory=lambda: deque(maxlen=100))
    line_count: int = 0
    _output_callbacks: list[Callable[[bytes], None]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def send_raw(self, data: str | bytes) -> None:
        if not self.tcp_connected or not self.tcp_socket:
            return
        if isinstance(data, str):
            data = data.encode()
        self.tcp_socket.sendall(data)

    def subscribe_output(self, cb: Callable[[bytes], None]) -> None:
        with self._lock:
            self._output_callbacks.append(cb)

    def unsubscribe_output(self, cb: Callable[[bytes], None]) -> None:
        with self._lock:
            try:
                self._output_callbacks.remove(cb)
            except ValueError:
                pass

    def dispatch_output(self, data: bytes) -> None:
        with self._lock:
            cbs = list(self._output_callbacks)
        for cb in cbs:
            cb(data)

    def to_dict(self) -> dict:
        """Return JSON-serializable representation (excludes process, socket, lock, callbacks)."""
        return {
            "vm_id": self.vm_id,
            "pid": self.pid,
            "state": self.state,
            "tcp_connected": self.tcp_connected,
            "created_at": self.created_at,
            "boot_time_ms": self.boot_time_ms,
            "ready_time_ms": self.ready_time_ms,
            "timings": dict(self.timings),
            "log_lines": list(self.log_lines),
            "line_count": self.line_count,
        }

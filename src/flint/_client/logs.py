"""Background WebSocket client for per-VM log streams (`/vms/{id}/logs`).

Same thread+WS pattern as EventStream and the existing terminal connection.
Each LogStream wraps a single VM's stream; consumers start one per selected
VM and stop it when switching away.
"""

from __future__ import annotations

import threading
from typing import Callable

import websockets.sync.client as ws_sync
from websockets.exceptions import WebSocketException

from flint.core.config import DAEMON_URL, log


class LogStream:
    def __init__(
        self,
        vm_id: str,
        on_line: Callable[[str], None],
        *,
        base_url: str = DAEMON_URL,
    ) -> None:
        self._on_line = on_line
        self._ws_url = base_url.replace("http://", "ws://").replace("https://", "wss://") + f"/vms/{vm_id}/logs"
        self._stop = threading.Event()
        self._ws = None
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name=f"flint-logs-{vm_id[:8]}")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass

    def _run_loop(self) -> None:
        try:
            self._ws = ws_sync.connect(self._ws_url, open_timeout=5)
        except Exception as e:
            log.debug("LogStream connect failed: %s", e)
            return
        try:
            for msg in self._ws:
                if self._stop.is_set():
                    break
                line = msg.decode("utf-8", errors="replace") if isinstance(msg, bytes) else msg
                try:
                    self._on_line(line)
                except Exception:
                    log.exception("LogStream on_line failed")
        except WebSocketException:
            pass
        except Exception:
            log.exception("LogStream read loop error")
        finally:
            try:
                self._ws.close()
            except Exception:
                pass

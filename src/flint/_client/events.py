"""Background WebSocket client for the daemon `/events` broadcast.

Mirrors the existing terminal-WS pattern: a daemon thread holds the socket
and invokes callbacks in that thread. The consumer is responsible for
marshaling onto the Textual event loop (typically via `App.call_from_thread`).

Auto-reconnects with exponential backoff. On successful reconnect the
`on_resync` callback fires so the consumer can refetch authoritative state —
events emitted while disconnected are gone.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Callable

import websockets.sync.client as ws_sync
from websockets.exceptions import WebSocketException

from flint.core.config import DAEMON_URL, log


class EventStream:
    def __init__(
        self,
        on_event: Callable[[dict], None],
        *,
        on_resync: Callable[[], None] | None = None,
        on_connected: Callable[[], None] | None = None,
        on_disconnected: Callable[[], None] | None = None,
        base_url: str = DAEMON_URL,
    ) -> None:
        self._on_event = on_event
        self._on_resync = on_resync
        self._on_connected = on_connected
        self._on_disconnected = on_disconnected
        self._ws_url = base_url.replace("http://", "ws://").replace("https://", "wss://") + "/events"
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="flint-events")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run_loop(self) -> None:
        backoff = 0.25
        first = True
        while not self._stop.is_set():
            try:
                ws = ws_sync.connect(self._ws_url, open_timeout=5)
            except Exception as e:
                log.debug("EventStream connect failed: %s", e)
                if self._stop.wait(backoff):
                    return
                backoff = min(backoff * 2, 5.0)
                continue

            backoff = 0.25
            if self._on_connected:
                try:
                    self._on_connected()
                except Exception:
                    log.exception("EventStream on_connected failed")
            if not first and self._on_resync:
                try:
                    self._on_resync()
                except Exception:
                    log.exception("EventStream on_resync failed")
            first = False

            try:
                for msg in ws:
                    if self._stop.is_set():
                        break
                    if isinstance(msg, bytes):
                        msg = msg.decode("utf-8", errors="replace")
                    try:
                        event = json.loads(msg)
                    except json.JSONDecodeError:
                        continue
                    try:
                        self._on_event(event)
                    except Exception:
                        log.exception("EventStream on_event failed")
            except WebSocketException:
                pass
            except Exception:
                log.exception("EventStream read loop error")
            finally:
                try:
                    ws.close()
                except Exception:
                    pass
                if self._on_disconnected:
                    try:
                        self._on_disconnected()
                    except Exception:
                        log.exception("EventStream on_disconnected failed")

            if self._stop.wait(backoff):
                return

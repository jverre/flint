"""Log stream test — subscribes to /vms/{id}/logs and verifies history delivery."""

from __future__ import annotations

import threading
import time


from .conftest import AUTH, BASE_URL, TEST_PORT, api, ws_connect




def _collect(lines: list, ws) -> None:
    try:
        for msg in ws:
            text = msg.decode() if isinstance(msg, bytes) else msg
            lines.append(text)
    except Exception:
        pass


def test_log_stream_delivers_lifecycle_lines(sandbox):
    # Daemon emits lifecycle log lines on create (timings etc.). Give it a tick.
    ws = ws_connect(f"/vms/{sandbox.id}/logs", open_timeout=5)
    lines: list = []
    t = threading.Thread(target=_collect, args=(lines, ws), daemon=True)
    t.start()

    deadline = time.time() + 2.0
    while time.time() < deadline and not lines:
        time.sleep(0.05)
    ws.close()
    assert lines, "Expected at least one log line from lifecycle events"
    assert any("VM created" in line for line in lines), lines

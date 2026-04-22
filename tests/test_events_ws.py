"""WebSocket test for /events: volume create/delete should round-trip."""

from __future__ import annotations

import json
import threading
import time

import websockets.sync.client as ws_sync
import httpx

from .conftest import TEST_PORT


BASE = f"http://127.0.0.1:{TEST_PORT}"
WS_BASE = f"ws://127.0.0.1:{TEST_PORT}"


def _collect(events: list, ws) -> None:
    try:
        for msg in ws:
            text = msg.decode() if isinstance(msg, bytes) else msg
            events.append(json.loads(text))
    except Exception:
        pass


def test_volume_events_broadcast(_ensure_daemon):
    ws = ws_sync.connect(f"{WS_BASE}/events", open_timeout=5)
    events: list = []
    t = threading.Thread(target=_collect, args=(events, ws), daemon=True)
    t.start()

    # Give the subscriber a moment to register.
    time.sleep(0.2)

    name = "flint-evt-vol"
    # Best-effort pre-cleanup.
    for v in httpx.get(f"{BASE}/volumes").json().get("volumes", []):
        if v.get("name") == name:
            httpx.delete(f"{BASE}/volumes/{v['id']}")

    resp = httpx.post(f"{BASE}/volumes", json={"name": name, "size_gib": 1}, timeout=5.0)
    assert resp.status_code == 200
    vol_id = resp.json()["volume"]["id"]

    try:
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if any(e.get("type") == "volume.created" for e in events):
                break
            time.sleep(0.05)
        assert any(e.get("type") == "volume.created" for e in events), events
    finally:
        httpx.delete(f"{BASE}/volumes/{vol_id}")
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if any(e.get("type") == "volume.deleted" for e in events):
                break
            time.sleep(0.05)
        assert any(e.get("type") == "volume.deleted" for e in events), events
        ws.close()

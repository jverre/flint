"""HTTP tests for the /volumes endpoints. Daemon only; no VM required."""

from __future__ import annotations

import os
import httpx

from .conftest import TEST_PORT


BASE = f"http://127.0.0.1:{TEST_PORT}"


def _cleanup(name: str) -> None:
    """Best-effort: find and delete any lingering volume with the given name."""
    try:
        r = httpx.get(f"{BASE}/volumes", timeout=5.0)
        for v in r.json().get("volumes", []):
            if v.get("name") == name:
                httpx.delete(f"{BASE}/volumes/{v['id']}", timeout=5.0)
    except Exception:
        pass


def test_volume_crud(_ensure_daemon):
    name = "flint-test-vol-1"
    _cleanup(name)

    # Create.
    resp = httpx.post(f"{BASE}/volumes", json={"name": name, "size_gib": 1}, timeout=10.0)
    assert resp.status_code == 200, resp.text
    vol = resp.json()["volume"]
    vol_id = vol["id"]
    try:
        assert vol["name"] == name
        assert vol["size_gib"] == 1
        assert os.path.exists(vol["image_path"])

        # List includes it.
        resp = httpx.get(f"{BASE}/volumes", timeout=5.0)
        assert resp.status_code == 200
        assert any(v["id"] == vol_id for v in resp.json()["volumes"])

        # Duplicate name → 400.
        resp = httpx.post(f"{BASE}/volumes", json={"name": name, "size_gib": 1}, timeout=5.0)
        assert resp.status_code == 400

        # Invalid size.
        resp = httpx.post(f"{BASE}/volumes", json={"name": "bad-1", "size_gib": -1}, timeout=5.0)
        assert resp.status_code == 400

    finally:
        r = httpx.delete(f"{BASE}/volumes/{vol_id}", timeout=5.0)
        assert r.status_code == 200

    # Deleting again → 404.
    resp = httpx.delete(f"{BASE}/volumes/{vol_id}", timeout=5.0)
    assert resp.status_code == 404

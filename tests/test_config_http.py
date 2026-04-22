"""HTTP tests for /config endpoints."""

from __future__ import annotations

import httpx

from .conftest import TEST_PORT


BASE = f"http://127.0.0.1:{TEST_PORT}"


def test_config_get_shape(_ensure_daemon):
    resp = httpx.get(f"{BASE}/config", timeout=5.0)
    assert resp.status_code == 200
    data = resp.json()
    assert "config" in data and "overrides" in data
    cfg = data["config"]
    for key in ("pool_target_size", "default_sandbox_timeout", "health_check_interval", "error_cleanup_delay"):
        assert key in cfg


def test_config_patch_roundtrip(_ensure_daemon):
    # Read current value.
    resp = httpx.get(f"{BASE}/config", timeout=5.0)
    overrides_before = resp.json()["overrides"]

    # Write an override.
    resp = httpx.patch(
        f"{BASE}/config",
        json={"default_sandbox_timeout": 123},
        timeout=5.0,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert data["overrides"].get("default_sandbox_timeout") == 123
    assert "default_sandbox_timeout" in data["requires_restart"]

    # Verify it persists in a subsequent GET.
    resp = httpx.get(f"{BASE}/config", timeout=5.0)
    assert resp.json()["overrides"].get("default_sandbox_timeout") == 123

    # Unknown field is rejected.
    resp = httpx.patch(f"{BASE}/config", json={"nope": 1}, timeout=5.0)
    assert resp.status_code == 400

    # Restore previous overrides (best-effort).
    if "default_sandbox_timeout" in overrides_before:
        httpx.patch(
            f"{BASE}/config",
            json={"default_sandbox_timeout": overrides_before["default_sandbox_timeout"]},
            timeout=5.0,
        )

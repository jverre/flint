"""Metrics endpoint test — requires a working sandbox."""

from __future__ import annotations

import time

import httpx

from .conftest import TEST_PORT


BASE = f"http://127.0.0.1:{TEST_PORT}"


def test_metrics_returns_samples(sandbox):
    # Wait for at least one sampler tick to land.
    time.sleep(2.0)
    resp = httpx.get(f"{BASE}/vms/{sandbox.id}/metrics", params={"window": 10}, timeout=5.0)
    assert resp.status_code == 200, resp.text
    samples = resp.json()["samples"]
    assert isinstance(samples, list)
    # On Linux we expect non-empty samples; on macOS we may get zero-fill but
    # the shape should be stable.
    if samples:
        s = samples[-1]
        assert "ts" in s and "cpu_percent" in s and "rss_bytes" in s

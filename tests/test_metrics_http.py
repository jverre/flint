"""Metrics endpoint test — requires a working sandbox."""

from __future__ import annotations

import time

from .conftest import BASE_URL, api


BASE = BASE_URL


def _samples(vm_id: str) -> list[dict]:
    resp = api.get(f"{BASE}/vms/{vm_id}/metrics", params={"window": 10}, timeout=5.0)
    assert resp.status_code == 200, resp.text
    return resp.json()["samples"]


def test_metrics_report_cpu_and_memory(sandbox):
    # Keep a vCPU busy for a while, in the background.
    sandbox.commands.run("sh -c 'end=$(($(date +%s)+20)); while [ $(date +%s) -lt $end ]; do :; done' >/dev/null 2>&1 &")
    deadline = time.time() + 15
    samples: list[dict] = []
    while time.time() < deadline:
        samples = _samples(sandbox.id)
        if any(s["cpu_percent"] > 20 for s in samples) and samples[-1]["rss_bytes"] > 0:
            break
        time.sleep(0.5)
    assert samples, "no metrics samples"
    assert samples[-1]["rss_bytes"] > 0, f"no memory reported: {samples[-3:]}"
    assert any(s["cpu_percent"] > 20 for s in samples), f"busy loop not visible in CPU: {[s['cpu_percent'] for s in samples]}"

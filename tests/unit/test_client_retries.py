"""The SDK's retry policy for calls that change something (create, kill, pause, resume, fork)."""

from __future__ import annotations

import httpx
import pytest

from flint._client.client import DaemonClient


def client(role: str, answers: list) -> tuple[DaemonClient, list[httpx.Request]]:
    """A client whose server answers /health with role, and each VM call with the next answer."""
    seen: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "role": role})
        seen.append(req)
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a

    c = DaemonClient(base_url="http://flint.test", api_key="", tenant="t")
    c._http = httpx.Client(base_url="http://flint.test", transport=httpx.MockTransport(handle))
    c.retry_for = 5.0
    return c, seen


VM = httpx.Response(200, json={"vm": {"vm_id": "vm-1"}})


def test_cluster_retries_with_the_same_key_until_the_call_lands(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    c, seen = client("control-plane", [
        httpx.ReadError("replica died"),
        httpx.Response(502, json={"detail": "bad gateway"}),
        httpx.Response(409, json={"detail": "another operation on this VM is in progress; retry shortly"}),
        VM,
    ])
    assert c.create()["vm_id"] == "vm-1"
    keys = {r.headers["Idempotency-Key"] for r in seen}
    assert len(seen) == 4 and len(keys) == 1, keys


def test_each_call_gets_its_own_key(monkeypatch):
    c, seen = client("control-plane", [VM, VM])
    c.create()
    c.create()
    assert seen[0].headers["Idempotency-Key"] != seen[1].headers["Idempotency-Key"]


def test_cluster_does_not_retry_answers_that_will_not_change(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    c, seen = client("control-plane", [
        httpx.Response(503, json={"detail": "VM creation failed: capacity_exhausted: no host"}),
        httpx.Response(409, json={"detail": "VM is Paused, not running"}),
    ])
    with pytest.raises(httpx.HTTPStatusError):
        c.create()
    with pytest.raises(httpx.HTTPStatusError):
        c.pause("vm-1")
    assert len(seen) == 2


def test_daemon_only_retries_requests_that_never_reached_it(monkeypatch):
    """A single daemon ignores Idempotency-Key, so a request it may have run isn't sent twice."""
    monkeypatch.setattr("time.sleep", lambda s: None)
    c, seen = client("daemon", [httpx.ConnectError("refused"), VM, httpx.ReadError("dropped")])
    assert c.create()["vm_id"] == "vm-1"
    with pytest.raises(httpx.ReadError):
        c.create()
    assert len(seen) == 3

"""Tenants and API tokens on a single daemon.

The daemon's socket is trusted (an admin); TCP needs a token. A service
token pinned to a tenant sees only that tenant's VMs, volumes and events:
the others behave as if they didn't exist.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid

import httpx
import pytest

from flint._client.client import DaemonClient
from flint._client.transport import ws_connect

from .conftest import BASE_URL, TCP_URL, TEST_STATE_DIR, _read_daemon_log, api


@pytest.fixture
def tenants(daemon_only):
    """Two fresh tenants, each with a service token pinned to it."""
    out = {}
    ids = []
    for label in ("a", "b"):
        tenant = f"t{label}-{uuid.uuid4().hex[:8]}"
        resp = api.post(f"{BASE_URL}/v1/tokens", json={"kind": "service", "name": f"test {tenant}", "tenants": [tenant]})
        assert resp.status_code == 200, resp.text
        ids.append(resp.json()["id"])
        out[label] = (tenant, resp.json()["token"])
    yield out
    for token_id in ids:
        api.delete(f"{BASE_URL}/v1/tokens/{token_id}")


def _tcp(token: str = "") -> httpx.Client:
    return httpx.Client(base_url=TCP_URL, headers={"Authorization": f"Bearer {token}"} if token else {}, timeout=30.0)


def _collect(events: list, ws) -> None:
    try:
        for msg in ws:
            events.append(json.loads(msg))
    except Exception:
        pass


def _wait_for(events: list, pred, what: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for e in list(events):
            if pred(e):
                return e
        time.sleep(0.05)
    raise AssertionError(f"no {what} within {timeout}s; got {events}\n--- daemon log tail ---\n{_read_daemon_log()[-3000:]}")


def test_tcp_needs_a_token(daemon_only):
    with _tcp() as c:
        assert c.get("/health").status_code == 200
        assert c.get("/ui/").status_code in (200, 503)  # the UI shell is public; it signs in itself
        assert c.get("/vms").status_code == 401
    with _tcp("flint_nope_wrong") as c:
        assert c.get("/vms").status_code == 401
    with open(os.path.join(TEST_STATE_DIR, "admin.token")) as f:
        admin = f.read().strip()
    with _tcp(admin) as c:
        assert c.get("/vms").status_code == 200
        assert c.get("/config").status_code == 200


def test_service_token_cant_administer(tenants):
    _tenant, token = tenants["a"]
    with _tcp(token) as c:
        assert c.get("/config").status_code == 403
        assert c.post("/v1/tokens", json={"kind": "admin"}).status_code == 403
        assert c.get("/v1/tokens").status_code == 403


def test_revoked_token_is_refused(daemon_only):
    resp = api.post(f"{BASE_URL}/v1/tokens", json={"kind": "service", "name": "short-lived"})
    token_id, token = resp.json()["id"], resp.json()["token"]
    with _tcp(token) as c:
        assert c.get("/vms").status_code == 200
        api.delete(f"{BASE_URL}/v1/tokens/{token_id}")
        assert c.get("/vms").status_code == 401
    assert token_id not in {t["id"] for t in api.get(f"{BASE_URL}/v1/tokens").json()["tokens"]}


def test_tenants_see_only_their_own_vms(tenants, require_sandbox):
    (ta, tok_a), (tb, tok_b) = tenants["a"], tenants["b"]
    a, b = DaemonClient(TCP_URL, api_key=tok_a), DaemonClient(TCP_URL, api_key=tok_b)
    events_a: list = []
    events_b: list = []
    ws_a = ws_connect(TCP_URL, "/events", {"Authorization": f"Bearer {tok_a}"}, open_timeout=5)
    ws_b = ws_connect(TCP_URL, "/events", {"Authorization": f"Bearer {tok_b}"}, open_timeout=5)
    for events, ws in ((events_a, ws_a), (events_b, ws_b)):
        threading.Thread(target=_collect, args=(events, ws), daemon=True).start()
    vm_a = vm_b = None
    try:
        vm_a = a.create()["vm_id"]  # the token's only tenant, without asking
        assert a.get(vm_a)["tenant"] == ta

        # Tenant b can't see, use or delete tenant a's VM: it doesn't exist for b.
        assert vm_a not in {v["vm_id"] for v in b.list()}
        assert b.get(vm_a) is None
        with _tcp(tok_b) as c:
            assert c.post(f"/vms/{vm_a}/exec", json={"cmd": ["true"]}).status_code == 404
            assert c.get(f"/vms/{vm_a}/files", params={"path": "/etc/hostname"}).status_code == 404
            assert c.delete(f"/vms/{vm_a}").status_code == 404
        for path in (f"/vms/{vm_a}/terminal", f"/vms/{vm_a}/logs"):
            with pytest.raises(Exception):
                ws_connect(TCP_URL, path, {"Authorization": f"Bearer {tok_b}"}, open_timeout=5).recv(timeout=5)
        assert a.exec_command(vm_a, "echo still-mine")["stdout"].strip() == "still-mine"

        # A token can't put a VM in a tenant it doesn't act for.
        with pytest.raises(httpx.HTTPStatusError) as e:
            a.create(tenant=tb)
        assert e.value.response.status_code == 403, e.value.response.text

        # Events: each tenant's stream carries only its own VMs. Streams are
        # ordered, so once b has seen its own VM's event, a's (earlier) would
        # have been delivered before it, had it not been filtered out.
        _wait_for(events_a, lambda e: e.get("type") == "vm.created" and e["vm"]["vm_id"] == vm_a, "vm.created for a's VM")
        vm_b = b.create()["vm_id"]
        _wait_for(events_b, lambda e: e.get("type") == "vm.created" and e["vm"]["vm_id"] == vm_b, "vm.created for b's VM")
        assert not [e for e in events_b if vm_a in json.dumps(e)], events_b
        assert all(e.get("tenant") == tb for e in events_b), events_b

        # The socket is an admin: it sees both, with their tenants, and can filter.
        listed = {v["vm_id"]: v["tenant"] for v in api.get(f"{BASE_URL}/vms").json()["vms"]}
        assert listed.get(vm_a) == ta and listed.get(vm_b) == tb, listed
        only_a = api.get(f"{BASE_URL}/vms", params={"tenant": ta}).json()["vms"]
        assert [v["vm_id"] for v in only_a] == [vm_a]
    finally:
        ws_a.close()
        ws_b.close()
        for vm in (vm_a, vm_b):
            if vm:
                api.delete(f"{BASE_URL}/vms/{vm}")


def test_volumes_belong_to_a_tenant(tenants, require_sandbox):
    (ta, tok_a), (_, tok_b) = tenants["a"], tenants["b"]
    created: list[str] = []
    vm_b = None
    with _tcp(tok_a) as a, _tcp(tok_b) as b:
        try:
            resp = a.post("/volumes", json={"name": "same-name"})
            if resp.status_code == 503:
                pytest.skip(f"volumes unavailable here: {resp.text}")
            assert resp.status_code == 200, resp.text
            vol_a = resp.json()["volume"]
            created.append(vol_a["id"])
            assert vol_a["tenant"] == ta

            # Names are per tenant: b can use the same one, and sees only its own.
            resp = b.post("/volumes", json={"name": "same-name"})
            assert resp.status_code == 200, resp.text
            created.append(resp.json()["volume"]["id"])
            assert [v["id"] for v in b.get("/volumes").json()["volumes"]] == [created[1]]

            # a's volume doesn't exist for b, by ID or by name.
            assert b.delete(f"/volumes/{vol_a['id']}").status_code == 404
            assert b.post(f"/volumes/{vol_a['id']}/fork", json={"name": "copy"}).status_code == 404
            # By name, b reaches its own volume, never a's.
            assert b.delete("/volumes/same-name").status_code == 200
            assert b.get("/volumes").json()["volumes"] == []
            assert vol_a["id"] in {v["id"] for v in a.get("/volumes").json()["volumes"]}

            # Not even an admin can mount a's volume into b's VM.
            vm_b = DaemonClient(TCP_URL, api_key=tok_b).create()["vm_id"]
            resp = api.post(f"{BASE_URL}/vms/{vm_b}/mounts", json={"volume": vol_a["id"], "path": "/mnt/a"})
            assert resp.status_code == 404, resp.text
        finally:
            if vm_b:
                api.delete(f"{BASE_URL}/vms/{vm_b}")
            for vol in reversed(created):
                api.delete(f"{BASE_URL}/volumes/{vol}")

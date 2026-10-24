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


def test_a_tenant_over_its_quota_is_refused(tenants, require_sandbox):
    (ta, tok_a), (_, tok_b) = tenants["a"], tenants["b"]
    a, b = DaemonClient(TCP_URL, api_key=tok_a), DaemonClient(TCP_URL, api_key=tok_b)
    with _tcp(tok_a) as c:
        assert c.put(f"/v1/tenants/{ta}/quota", json={"max_vms": 1}).status_code == 403  # admins set quotas
    resp = api.put(f"{BASE_URL}/v1/tenants/{ta}/quota", json={"max_vms": 1})
    assert resp.status_code == 200, resp.text
    vms = []
    try:
        vms.append(a.create()["vm_id"])
        with pytest.raises(httpx.HTTPStatusError) as e:
            a.create()
        assert e.value.response.status_code == 429, e.value.response.text
        assert "quota" in e.value.response.json()["detail"], e.value.response.text
        vms.append(b.create()["vm_id"])  # another tenant isn't limited by it

        with _tcp(tok_a) as c:
            q = c.get(f"/v1/tenants/{ta}/quota").json()
            assert (q["vms"], q["max_vms"]) == (1, 1), q
            assert q["vcpus"] >= 1 and q["memory_mib"] >= 128, q

        # Deleting the VM frees its place.
        a.kill(vms.pop(0))
        vms.append(a.create()["vm_id"])
    finally:
        for vm in vms:
            api.delete(f"{BASE_URL}/vms/{vm}")


def test_two_tenants_are_isolated(tenants, require_sandbox, backend_health):
    """Phase 1's exit test: every boundary between two tenants, on one host.

    Tenant a has two VMs (one holding a credential), tenant b one. None can
    reach another, the host or private ranges; the internet works; a's
    credential is injected on a's egress only, and is never inside a VM nor
    readable through the API; deleting a's VM wipes it, and only a hears so.
    """
    if not backend_health.get("network_policy"):
        pytest.skip(f"{backend_health['backend_kind']} has no bridge network or credential proxy")
    from flint.core.config import BRIDGE_IP, PROXY_PORT, VETH_SUBNET

    (ta, tok_a), (tb, tok_b) = tenants["a"], tenants["b"]
    a, b = DaemonClient(TCP_URL, api_key=tok_a), DaemonClient(TCP_URL, api_key=tok_b)
    secret = f"secret-{uuid.uuid4().hex}"
    policy = {"allow": {"postman-echo.com": [{"transform": [{"headers": {"X-Flint-Auth": secret}}]}]}}
    events_a: list = []
    events_b: list = []
    ws_a = ws_connect(TCP_URL, "/events", {"Authorization": f"Bearer {tok_a}"}, open_timeout=5)
    ws_b = ws_connect(TCP_URL, "/events", {"Authorization": f"Bearer {tok_b}"}, open_timeout=5)
    for events, ws in ((events_a, ws_a), (events_b, ws_b)):
        threading.Thread(target=_collect, args=(events, ws), daemon=True).start()
    vms: list[str] = []
    volumes: list[str] = []
    try:
        a1 = a.create(network_policy=policy)["vm_id"]
        vms.append(a1)
        a2 = a.create()["vm_id"]
        vms.append(a2)
        vm_b = b.create()["vm_id"]
        vms.append(vm_b)

        def run(client, vm, cmd, timeout=60):
            r = client.exec_command(vm, cmd, timeout=timeout)
            return r["exit_code"], r["stdout"] + r["stderr"]

        # Network: on the bridge only a1 itself answers, so neither a's other
        # VM nor b's can be reached, nor anything of theirs (their proxies included).
        code, out = run(a, a1, f"for i in $(seq 2 254); do (ping -c1 -W1 {VETH_SUBNET}.$i >/dev/null 2>&1 "
                               f"&& echo {VETH_SUBNET}.$i) & done; wait")
        assert len(out.split()) == 1, f"bridge addresses answering a1: {out}"
        # Raw TCP from a2: a1's ports 80 and 443 go to its credential proxy,
        # which accepts any connection and only then dials out.
        for target in (f"{BRIDGE_IP} 22", f"{BRIDGE_IP} {PROXY_PORT}", "169.254.169.254 80",
                       "192.168.0.1 80", "172.17.0.1 80", "100.64.0.1 80"):
            code, out = run(a, a2, f"nc -w3 {target} </dev/null")
            assert code != 0, f"a2 reached {target}: {out}"
        code, out = run(a, a2, "nc -w5 1.1.1.1 80 </dev/null")
        assert code == 0, f"a2 can't reach the internet: {out}"
        # Through a1's proxy, cloud metadata is still out of reach.
        code, out = run(a, a1, "wget -q -T5 -O - --header 'Metadata: true' "
                               "'http://169.254.169.254/metadata/instance?api-version=2021-02-01'")
        assert code != 0, f"a1 got cloud metadata through its proxy: {out}"

        # Default deny includes real guest services and the two control ports.
        peer_ip = a.get(a2)["network_address"]
        other_ip = b.get(vm_b)["network_address"]
        assert peer_ip and other_ip and peer_ip != other_ip
        code, out = run(a, a2, "mkdir -p /tmp/peer-www; echo tenant-a > /tmp/peer-www/index.html; "
                             "cd /tmp/peer-www; nohup python3 -m http.server 18080 >/tmp/peer-http.log 2>&1 </dev/null &")
        assert code == 0, out
        code, out = run(a, a2, "for i in $(seq 1 50); do "
                             "wget -q -T1 -O - http://127.0.0.1:18080 && exit 0; sleep .1; done; "
                             "cat /tmp/peer-http.log; exit 1")
        assert code == 0 and out.strip() == "tenant-a", out
        code, out = run(a, a1, f"wget -q -T2 -O - http://{peer_ip}:18080")
        assert code != 0, f"same-tenant traffic allowed without opt-in: {out}"
        with _tcp(tok_b) as c:
            assert c.put(f"/v1/tenants/{ta}/network", json={"allow_same_tenant": True}).status_code == 403
        with _tcp(tok_a) as c:
            c.put(f"/v1/tenants/{ta}/network", json={"allow_same_tenant": True}).raise_for_status()
            assert c.get(f"/v1/tenants/{ta}/network").json()["allow_same_tenant"] is True
        code, out = run(a, a1, f"wget -q -T3 -O - http://{peer_ip}:18080")
        assert code == 0 and out.strip() == "tenant-a", out
        for target in (f"{peer_ip} 5000", f"{peer_ip} {PROXY_PORT}", f"{other_ip} 18080"):
            code, out = run(a, a1, f"nc -w2 {target} </dev/null")
            assert code != 0, f"peer opt-in exposed another tenant or control port: {target}: {out}"
        a.pause(a2)
        a.resume(a2)
        code, out = run(a, a1, f"wget -q -T3 -O - http://{peer_ip}:18080")
        assert code == 0 and out.strip() == "tenant-a", out
        with _tcp(tok_a) as c:
            c.put(f"/v1/tenants/{ta}/network", json={"allow_same_tenant": False}).raise_for_status()
        code, out = run(a, a1, f"wget -q -T2 -O - http://{peer_ip}:18080")
        assert code != 0, f"traffic still allowed after revocation: {out}"

        # Credentials: injected on a1's HTTPS, never inside a1, not readable, not b's.
        code, out = run(a, a1, "wget -q -O - https://postman-echo.com/headers", timeout=30)
        assert code == 0, out
        assert json.loads(out)["headers"].get("x-flint-auth") == secret, out
        # The pattern is put together in the shell, so the probe itself never
        # brings the whole secret into the VM.
        half = len(secret) // 2
        code, out = run(a, a1, f'p1={secret[:half]}; p2={secret[half:]}; env | grep -c "$p1$p2"; '
                               'find / -xdev -type f -size -20M 2>/dev/null | xargs grep -l "$p1$p2" 2>/dev/null', timeout=120)
        assert out.split() == ["0"], f"the secret is inside a1 (env match count, then files): {out}"
        assert secret not in json.dumps(a.get_network_policy(a1))
        assert secret not in json.dumps(api.get(f"{BASE_URL}/vms").json())
        code, out = run(b, vm_b, "wget -q -O - https://postman-echo.com/headers", timeout=30)
        assert code == 0, out
        assert "x-flint-auth" not in json.loads(out)["headers"], out
        assert b.get(a1) is None

        # Storage ownership is enforced even for an admin attaching a volume.
        with _tcp(tok_a) as c:
            response = c.post("/volumes", json={"name": "isolation-data", "size_gib": 1})
            response.raise_for_status()
            volume = response.json()["volume"]
            volumes.append(volume["id"])
        assert volume["tenant"] == ta, volume
        api.post(f"{BASE_URL}/vms/{a1}/mounts", json={"volume": volume["id"], "path": "/mnt/private"}).raise_for_status()
        code, out = run(a, a1, "echo tenant-a-data > /mnt/private/marker; sync")
        assert code == 0, out
        assert api.post(f"{BASE_URL}/vms/{vm_b}/mounts", json={"volume": volume["id"], "path": "/mnt/stolen"}).status_code == 404
        with _tcp(tok_b) as c:
            assert volume["id"] not in {v["id"] for v in c.get("/volumes").json()["volumes"]}
            assert c.post(f"/volumes/{volume['id']}/snapshot", json={"name": "stolen"}).status_code == 404
        api.delete(f"{BASE_URL}/vms/{a1}/mounts", params={"path": "/mnt/private"}).raise_for_status()
        from pathlib import Path
        from flint.core._nfs_server import IMAGES_DIR, VOLUMES_DIR
        volume_dir = Path(IMAGES_DIR if volume["kind"] == "image" else VOLUMES_DIR) / volume["id"]
        volume_files = [p.open("rb") for p in volume_dir.rglob("*") if p.is_file() and not p.is_symlink()
                        and p.stat().st_nlink == 1 and p.stat().st_size > 0]
        assert volume_files, f"no on-disk volume files exercised under {volume_dir}"
        try:
            with _tcp(tok_a) as c:
                c.delete(f"/volumes/{volume['id']}").raise_for_status()
            volumes.remove(volume["id"])
            assert not volume_dir.exists()
            for stream in volume_files:
                while chunk := stream.read(1 << 20):
                    assert chunk.count(0) == len(chunk), f"data left in deleted volume file {stream.name}"
        finally:
            for stream in volume_files:
                stream.close()

        # Hold the real disk/snapshot inodes open across deletion, so an
        # unlink without wiping cannot satisfy the exit criterion.
        import sqlite3
        from pathlib import Path
        a.pause(a1)
        with sqlite3.connect(os.path.join(TEST_STATE_DIR, "flint.db")) as db:
            vm_dir, chroot = db.execute("SELECT vm_dir, chroot_base FROM sandboxes WHERE vm_id = ?", (a1,)).fetchone()
        root = Path(chroot or vm_dir)
        private = [p for p in root.rglob("*") if not p.is_symlink() and p.is_file()
                   and p.stat().st_nlink == 1 and p.stat().st_size > 0]
        assert any("pause" in str(p) for p in private), "no private pause snapshot was exercised"
        held = [p.open("rb") for p in private]
        try:
            a.kill(a1)
            vms.remove(a1)
            wiped = _wait_for(events_a, lambda e: e.get("type") == "vm.wiped" and e.get("vm_id") == a1, "vm.wiped for a1", 30)
            assert wiped["files"] > 0 and wiped["bytes"] > 0, wiped
            assert not root.exists(), f"deleted VM files remain under {root}"
            for stream in held:
                while chunk := stream.read(1 << 20):
                    assert chunk.count(0) == len(chunk), f"data left in deleted file {stream.name}"
        finally:
            for stream in held:
                stream.close()
        b_mark = b.create()["vm_id"]  # once b hears of its own VM, it would have heard of a1 before
        vms.append(b_mark)
        _wait_for(events_b, lambda e: e.get("type") == "vm.created" and e["vm"]["vm_id"] == b_mark, "b's own event")
        assert not [e for e in events_b if a1 in json.dumps(e)], events_b

        # Quota: a tenant at its limit gets no more VMs; the other tenant still does.
        held = api.get(f"{BASE_URL}/v1/tenants/{ta}/quota").json()["vms"]
        api.put(f"{BASE_URL}/v1/tenants/{ta}/quota", json={"max_vms": held}).raise_for_status()
        with pytest.raises(httpx.HTTPStatusError) as e:
            a.create()
        assert e.value.response.status_code == 429, e.value.response.text
        vms.append(b.create()["vm_id"])
        assert api.get(f"{BASE_URL}/v1/tenants/{tb}/quota").json()["vms"] == 3
    finally:
        ws_a.close()
        ws_b.close()
        for vm in vms:
            api.delete(f"{BASE_URL}/vms/{vm}")
        for volume_id in volumes:
            api.delete(f"{BASE_URL}/volumes/{volume_id}")

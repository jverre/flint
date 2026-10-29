"""Cluster e2e: the flows in plans/03-cluster.md, against real hosts and VMs.

Run as root:  sudo -E .venv/bin/python -m pytest tests/cluster -v
"""

import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
import websockets.sync.client as ws_sync

from flint import Sandbox

from .conftest import TENANT, host_of
from .harness import raw_join, wait_for, host_client_ctx


def state(sb: Sandbox) -> dict:
    return sb._fetch() or {}


def running(pid: int) -> bool:
    """A killed VMM whose parent (the agent) is frozen stays a zombie: that's dead."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


def test_host_joins_once_and_reports_capacity(cluster, hosts):
    for h in hosts.values():
        info = h.info()
        assert info["state"] == "ready"
        assert info["capacity"]["max_vms"] > 0 and info["capacity"]["mem_total_mb"] > 0
        assert info["host_class"].startswith(info["backend"])
    zones = {h.info()["zone"] for h in hosts.values()}
    assert zones == {"zone-1", "zone-2"}
    token = cluster.join_token("zone-9")
    assert raw_join(cluster, token) == 200, "a fresh token should let a host join"
    assert raw_join(cluster, token) == 403, "a join token must be single-use"
    intruder = [h for h in cluster.api.get("/v1/hosts").json()["hosts"] if h["name"] == "intruder"]
    for h in intruder:
        cluster.api.delete(f"/v1/hosts/{h['id']}?force=true")


def test_vms_spread_across_hosts_and_work_through_the_gateway(cluster, make_vm):
    vms = [make_vm() for _ in range(4)]
    assert {host_of(cluster, v) for v in vms} == {"a", "b"}, "the scheduler should use both hosts"
    for v in vms:
        r = v.commands.run("echo $((6*7)); echo err >&2; exit 3")
        assert (r.stdout.strip(), r.stderr.strip(), r.exit_code) == ("42", "err", 3)
        v.write_file("/root/f.txt", v.id)
        assert v.read_file("/root/f.txt") == v.id.encode()
        assert any(e["name"] == "f.txt" for e in v.list_files("/root"))
    # A PTY through the control plane's gateway (WebSocket proxied to the host).
    term = ws_sync.connect(f"{cluster.url.replace('http', 'ws')}/vms/{vms[0].id}/terminal",
                           additional_headers=dict(cluster.api.headers))
    try:
        term.send(b"echo pty-$((40+2))\n")
        out = b""
        deadline = time.time() + 15
        while b"pty-42" not in out and time.time() < deadline:
            out += term.recv(timeout=5)
        assert b"pty-42" in out, out[-300:]
    finally:
        term.close()


def test_fork_lands_on_another_host_when_the_source_host_is_full(cluster, hosts, make_vm):
    src = make_vm(on="a")
    # Detach the sleeper's streams: inherited output pipes keep commands.run
    # waiting until its 60s timeout even after the shell exits.
    r = src.commands.run("echo before-fork > /root/state; (sleep 600 </dev/null >/dev/null 2>&1 &); true")
    assert r.exit_code == 0, r
    src_pids = src.commands.run("pgrep -f 'sleep 600'").stdout.split()
    assert src_pids, "background process didn't start"
    cluster.api.patch(f"/v1/hosts/{hosts['a'].host_id}", json={"max_vms": 1})  # a is now full
    t0 = time.time()
    kids = src.fork(2)
    make_vm.extra = kids
    took = time.time() - t0
    assert [host_of(cluster, k) for k in kids] == ["b", "b"], cluster.diagnostics()
    for k in kids:
        assert k.read_file("/root/state") == b"before-fork\n"
        # Memory came along: the parent's running process is in the child.
        assert k.commands.run("pgrep -f 'sleep 600'").stdout.split() == src_pids
        k.write_file("/root/state", k.id)  # children diverge from each other and the parent
    assert src.read_file("/root/state") == b"before-fork\n"
    assert src.commands.run("echo alive").stdout.strip() == "alive", "the source must keep running"
    assert took < 30, f"fork took {took:.1f}s"


def test_paused_vm_resumes_on_another_host(cluster, hosts, make_vm):
    vm = make_vm(on="a")
    r = vm.commands.run("echo paused-state > /root/p; (sleep 700 </dev/null >/dev/null 2>&1 &); true")
    assert r.exit_code == 0, r
    pid = vm.commands.run("pgrep -f 'sleep 700'").stdout.strip()
    vm.pause()
    assert state(vm)["state"] == "Paused"
    cluster.api.post(f"/v1/hosts/{hosts['a'].host_id}/cordon")
    vm.resume()
    assert host_of(cluster, vm) == "b"
    assert vm.read_file("/root/p") == b"paused-state\n"
    assert vm.commands.run("pgrep -f 'sleep 700'").stdout.strip() == pid, "processes survive the move"
    # The stale paused copy on a is gone.
    wait_for(lambda: vm.id not in hosts["a"].agent_vms(), 30, "host a to drop its paused copy")


def test_host_crash_recovers_vms_by_availability_class(cluster, hosts, make_vm):
    vms = {a: make_vm(on="b", availability=a) for a in ("ephemeral", "restartable", "durable")}
    wait_for(lambda: state(vms["durable"]).get("checkpoint_at"), 30, "the durable VM's first automatic checkpoint")
    for a, v in vms.items():
        v.write_file("/root/marker", a)
    # An automatic checkpoint may predate the writes. Complete a new one
    # after them so the recovery assertion has a known recovery point.
    checkpoint = cluster.api.post(f"/vms/{vms['durable'].id}/checkpoint")
    assert checkpoint.status_code == 200, checkpoint.text
    hosts["b"].crash()
    t0 = time.time()
    try:
        wait_for(lambda: state(vms["ephemeral"]).get("state") == "Lost", 40, "ephemeral VM marked lost")
        for a in ("restartable", "durable"):
            wait_for(lambda: state(vms[a]).get("state") == "Started" and host_of(cluster, vms[a]) == "a", 40, f"{a} VM recovered on a")
    except TimeoutError as e:
        pytest.fail(f"{e}\n{cluster.diagnostics()}")
    took = time.time() - t0
    # Durable: restored from its checkpoint, files included; restartable: fresh from its template.
    assert vms["durable"].read_file("/root/marker") == b"durable"
    assert vms["restartable"].commands.run("cat /root/marker").exit_code != 0
    assert vms["durable"].id == state(vms["durable"])["vm_id"], "a recovered VM keeps its ID"
    assert took < 30, f"recovery took {took:.1f}s (lease 6s + margin 3s)"
    assert hosts["b"].state() == "down"
    events = cluster.api.get("/v1/events", params={"since": 0}).json()["events"]
    kinds = {(e["type"], e.get("vm_id")) for e in events}
    assert ("vm.lost", vms["ephemeral"].id) in kinds
    assert ("vm.recovered", vms["durable"].id) in kinds
    # When b comes back it must not resurrect the VMs that moved.
    hosts["b"].ensure_up()
    wait_for(lambda: not set(hosts["b"].agent_vms()) & {v.id for v in vms.values()}, 30, "host b to evict stale VMs")


def test_hung_agent_is_fenced_before_its_vms_are_recovered(cluster, hosts, make_vm):
    durable = make_vm(on="b", availability="durable")
    ephemeral = make_vm(on="b")
    durable.write_file("/root/marker", "durable")
    checkpoint = cluster.api.post(f"/vms/{durable.id}/checkpoint")
    assert checkpoint.status_code == 200, checkpoint.text
    pid = state(durable)["pid"]
    # Started right before the hang, most likely between two heartbeats: it
    # must be fenced from the moment it exists, not from the next heartbeat.
    fresh = make_vm(on="b", availability="restartable")
    fresh_pid = state(fresh)["pid"]
    hosts["b"].freeze()  # the agent hangs; snapd (the fence) keeps running
    t0 = time.time()
    killed_at = recovered_at = None

    def watch_pid():  # its own thread: API calls below can be slow while b hangs
        nonlocal killed_at
        while killed_at is None and time.time() - t0 < 40:
            if not running(pid) and not running(fresh_pid):
                killed_at = time.time() - t0
            time.sleep(0.05)

    watcher = threading.Thread(target=watch_pid, daemon=True)
    watcher.start()
    while time.time() - t0 < 40 and recovered_at is None:
        st = state(durable)
        if st.get("state") == "Started" and host_of(cluster, durable) == "a":
            recovered_at = time.time() - t0
        time.sleep(0.1)
    watcher.join(max(0.0, 40 - (time.time() - t0)))
    assert killed_at is not None, (
        f"the fence never killed {'the restartable VM started just before the hang' if running(fresh_pid) else 'the durable VM'}"
        f"\n{cluster.diagnostics()}")
    assert recovered_at is not None, f"durable VM not recovered\n{cluster.diagnostics()}"
    assert killed_at < recovered_at, f"recovered at {recovered_at:.1f}s before the old copy was fenced at {killed_at:.1f}s"
    assert durable.read_file("/root/marker") == b"durable"
    # The host still writes storage heartbeats: isolated, not down; its ephemeral VM keeps running.
    assert hosts["b"].state() == "isolated"
    assert state(ephemeral)["cluster_state"] == "running"
    hosts["b"].thaw()
    wait_for(lambda: hosts["b"].state() == "ready", 30, "host b back")
    assert ephemeral.commands.run("echo still-here").stdout.strip() == "still-here"
    wait_for(lambda: durable.id not in hosts["b"].agent_vms(), 30, "host b to evict the fenced copy")


def test_restarted_agent_kills_vms_whose_lease_lapsed(cluster, hosts, make_vm):
    """Agent and snapd die but the VMs keep running (no watchdog): when the
    agent comes back after its lease lapsed, the copies that were recovered
    elsewhere must not keep running here."""
    durable = make_vm(on="b", availability="durable")
    durable.write_file("/root/marker", "durable")
    checkpoint = cluster.api.post(f"/vms/{durable.id}/checkpoint")
    assert checkpoint.status_code == 200, checkpoint.text
    old_pid = state(durable)["pid"]
    for p in (hosts["b"].agent, hosts["b"].snapd):
        p.kill()
    assert running(old_pid), "the VM should outlive its agent"
    wait_for(lambda: state(durable).get("state") == "Started" and host_of(cluster, durable) == "a", 40,
             "the durable VM recovered on a")
    hosts["b"].ensure_up()
    assert not running(old_pid), "the lapsed copy on b is still running"
    assert durable.read_file("/root/marker") == b"durable"


def test_control_plane_survives_losing_a_replica(cluster, hosts, make_vm):
    vm = make_vm()
    leader = next(i for i, p in enumerate(cluster.servers)
                  if httpx.get(f"http://127.0.0.1:{cluster.port + 10 + i}/health").json()["leader"])
    cluster.servers[leader].kill()
    try:
        # Requests through the load balancer keep working on the other replica.
        assert vm.commands.run("echo ok").stdout.strip() == "ok"
        other = make_vm()
        assert other.commands.run("echo new").stdout.strip() == "new"
        survivor = 1 - leader
        wait_for(lambda: httpx.get(f"http://127.0.0.1:{cluster.port + 10 + survivor}/health").json()["leader"],
                 15, "the surviving replica to take the reconciler lease")
    finally:
        cluster.servers[leader].start()
        wait_for(lambda: httpx.get(f"http://127.0.0.1:{cluster.port + 10 + leader}/health").status_code == 200, 30, "replica restart")


def replica_api(cluster, i: int) -> httpx.Client:
    """Talk to one control-plane replica directly, bypassing the load balancer."""
    return httpx.Client(base_url=f"http://127.0.0.1:{cluster.port + 10 + i}", headers=cluster.api.headers, timeout=180)


def copies(hosts, vm_id: str) -> list[str]:
    return [n for n, h in hosts.items() if vm_id in h.agent_vms()]


def test_concurrent_operations_on_one_vm_through_two_replicas_are_serialised(cluster, hosts, make_vm):
    vm = make_vm(on="a")
    r = vm.commands.run("echo before > /root/m; (sleep 900 </dev/null >/dev/null 2>&1 &); true")
    assert r.exit_code == 0, r
    results = {}

    def call(name, i, path):
        results[name] = replica_api(cluster, i).post(f"/vms/{vm.id}/{path}")

    threads = [threading.Thread(target=call, args=("migrate", 0, "migrate")),
               threading.Thread(target=call, args=("pause", 1, "pause"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    codes = {k: (r.status_code, r.text[:300]) for k, r in results.items()}
    # Each request either ran (one after the other) or was told the VM was busy.
    assert all(c in (200, 409) for c, _ in codes.values()), codes
    assert any(c == 200 for c, _ in codes.values()), codes
    final = state(vm)
    expected = "Paused" if results["pause"].status_code == 200 else "Started"
    assert final["state"] == expected, (codes, final)
    if final["state"] == "Paused":
        vm.resume()
    assert vm.read_file("/root/m") == b"before\n"
    assert vm.commands.run("pgrep -f 'sleep 900'").stdout.strip(), "the VM's processes survived"
    wait_for(lambda: copies(hosts, vm.id) == [host_of(cluster, vm)], 30,
             f"exactly one copy of the VM, on its catalog host (copies: {copies(hosts, vm.id)})")


def test_move_interrupted_by_a_replica_crash_is_finished_by_the_leader(cluster, hosts, make_vm):
    vm = make_vm(on="a")
    vm.commands.run("echo kept > /root/m; true")
    health = lambda i: httpx.get(f"http://127.0.0.1:{cluster.port + 10 + i}/health", timeout=5).json()
    victim = next(i for i in range(len(cluster.servers)) if not health(i)["leader"])

    def migrate():
        with replica_api(cluster, victim) as api:
            return api.post(f"/vms/{vm.id}/migrate", timeout=30)

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            # Freeze the target so the restore hangs and the move is caught half-way.
            hosts["b"].freeze()
            try:
                request = executor.submit(migrate)
                wait_for(lambda: state(vm).get("cluster_state") == "migrating", 15, "the move to start")
                cluster.servers[victim].kill()
            finally:
                hosts["b"].thaw()
            # Killing the serving replica closes the pending HTTP request (EOF or reset).
            # Observe its exception here so unexpected worker failures fail the test.
            with pytest.raises((httpx.RemoteProtocolError, httpx.ReadError)):
                request.result(timeout=10)
        recovered = lambda: [e for e in cluster.api.get("/v1/events", params={"since": 0, "type": "operation.recovered"}).json()["events"]
                             if e.get("vm_id") == vm.id]
        wait_for(lambda: recovered(), 40, "the leader to take over the interrupted move")
        wait_for(lambda: state(vm).get("state") == "Started", 30, f"the VM to run again ({state(vm)})")
        assert vm.read_file("/root/m") == b"kept\n"
        wait_for(lambda: copies(hosts, vm.id) == [host_of(cluster, vm)], 30,
                 f"exactly one copy of the VM, on its catalog host (copies: {copies(hosts, vm.id)})")
        # The VM takes new operations: its lock was released.
        vm.pause()
        vm.resume()
        assert vm.commands.run("cat /root/m").stdout == "kept\n"
    finally:
        cluster.servers[victim].start()
        wait_for(lambda: httpx.get(f"http://127.0.0.1:{cluster.port + 10 + victim}/health").status_code == 200, 30, "replica restart")


def test_create_retried_with_an_idempotency_key_makes_one_vm(cluster, hosts):
    body = {"tenant": TENANT}
    headers = {"Idempotency-Key": f"create-{time.time_ns()}"}
    # Through the load balancer, so the retry lands on the other replica.
    first = cluster.api.post("/vms", json=body, headers=headers)
    assert first.status_code == 200, first.text
    vm_id = first.json()["vm"]["vm_id"]
    try:
        again = cluster.api.post("/vms", json=body, headers=headers)
        assert again.status_code == 200 and again.headers.get("idempotent-replayed") == "true", again.text
        assert again.json()["vm"]["vm_id"] == vm_id
        mine = [v for v in cluster.api.get("/vms", params={"tenant": TENANT}).json()["vms"] if v["vm_id"] == vm_id]
        assert len(mine) == 1
        other = cluster.api.post("/vms", json={"tenant": TENANT, "template_id": "nope"}, headers=headers)
        assert other.status_code == 422, other.text
    finally:
        cluster.api.delete(f"/vms/{vm_id}")


def test_vmset_keeps_replicas_spread_and_promotes_a_standby_on_failure(cluster, hosts):
    r = cluster.api.post("/v1/vmsets", json={"name": "web", "tenant": TENANT,
                                             "spec": {"replicas": 2, "standby": 1, "spread": "zone"}})
    assert r.status_code == 200, r.text
    vs = r.json()["vmset"]["id"]
    get = lambda: cluster.api.get(f"/v1/vmsets/{vs}").json()["vmset"]
    try:
        wait_for(lambda: get()["status"]["ready"] and get()["status"]["standby"] == 1, 60, "the VM set to converge")
        st = get()["status"]
        assert st["by_zone"] == {"zone-1": 1, "zone-2": 1}, st
        members = get()["members"]
        standby = next(m for m in members if m["role"] == "standby")
        victim = next(m for m in members if m["role"] == "" and m["host_id"] != standby["host_id"])
        victim_host = next(h for h in hosts.values() if h.host_id == victim["host_id"])
        victim_host.crash()
        wait_for(lambda: any(e["type"] == "vmset.standby_promoted" and e.get("vm_id") == standby["vm_id"]
                             for e in cluster.api.get("/v1/events", params={"since": 0}).json()["events"]),
                 40, "the standby to be promoted")
        wait_for(lambda: get()["status"]["running"] == 2, 40, "two running replicas again")
        assert victim["vm_id"] not in [m["vm_id"] for m in get()["members"]]
    finally:
        cluster.api.delete(f"/v1/vmsets/{vs}")


def test_drain_moves_running_vms_with_their_state(cluster, hosts, make_vm):
    vms = [make_vm(on="a") for _ in range(2)]
    for v in vms:
        r = v.commands.run(f"echo {v.id} > /root/id; (sleep 800 </dev/null >/dev/null 2>&1 &); true")
        assert r.exit_code == 0, r
    r = cluster.api.post(f"/v1/hosts/{hosts['a'].host_id}/drain")
    assert r.status_code == 200
    wait_for(lambda: hosts["a"].state() == "drained", 60, "host a to drain")
    for v in vms:
        assert host_of(cluster, v) == "b"
        assert v.read_file("/root/id").strip() == v.id.encode()
        assert v.commands.run("pgrep -f 'sleep 800'").stdout.strip(), "processes survive a drain"
    moved = [e for e in cluster.api.get("/v1/events", params={"since": 0, "type": "vm.moved"}).json()["events"]
             if e.get("reason") == "drain" and e.get("vm_id") in {v.id for v in vms}]
    assert len(moved) == 2 and all(e["downtime_ms"] < 10000 for e in moved), moved


def test_credentials_and_tenants_are_isolated(cluster, hosts, make_vm):
    # A host certificate is not an API credential.
    req = urllib.request.Request(f"{cluster.url}/vms")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert e.value.code == 401
    # A host can't drive another host's agent: agents only accept control-plane certificates.
    a, b = hosts["a"], hosts["b"]
    ctx = host_client_ctx(a)
    ctx.load_verify_locations(os.path.join(a.identity, "hosts-ca.pem"))
    with pytest.raises((ssl.SSLError, urllib.error.URLError, ConnectionResetError)):
        urllib.request.urlopen(f"https://127.0.0.1:{b.agent_port}/vms", context=ctx, timeout=5)
    # A service token pinned to a tenant sees only its tenant.
    tok = cluster.api.post("/v1/tokens", json={"kind": "service", "tenants": ["other"]}).json()["token"]
    mine = make_vm()
    pinned = httpx.Client(base_url=cluster.url, headers={"Authorization": f"Bearer {tok}"})
    assert pinned.get(f"/vms/{mine.id}").status_code == 404
    assert all(v["tenant"] == "other" for v in pinned.get("/vms").json()["vms"])
    assert pinned.post("/vms", params={"tenant": TENANT}).status_code == 403
    # A tenant at its quota gets a clear refusal, not a VM.
    assert cluster.api.put("/v1/tenants/quota-t/quota", json={"max_vms": 1}).status_code == 200
    first = make_vm(tenant="quota-t")
    r = cluster.api.post("/vms", params={"tenant": "quota-t"})
    assert r.status_code == 429 and "quota" in r.json()["detail"], r.text
    with pytest.raises(RuntimeError, match="quota"):
        first.fork(1)
    # VMs can't reach the control plane, host agents or snapd on the host.
    bridge_ip = f"{b.subnet}.1"
    for port in (cluster.port, cluster.port + 1, b.agent_port, b.snapd_port):
        r = mine.commands.run(f"nc -w2 {bridge_ip} {port} </dev/null")
        assert r.exit_code != 0, f"VM reached host port {port}"


@pytest.mark.slow
def test_template_imported_on_one_host_boots_on_another(cluster, hosts, make_vm):
    name = "cluster-e2e-tpl"

    def import_image(image: str) -> tuple[str, dict]:
        r = cluster.api.post("/templates", json={"name": name, "tenant": TENANT, "image": image, "rootfs_size_mb": 1024})
        assert r.status_code == 200, r.text
        tid, importer = r.json()["template_id"], r.json()["build_host"]
        get = lambda: cluster.api.get(f"/templates/{tid}").json()["template"]
        wait_for(lambda: get()["status"] in ("ready", "error"), 900, "the template import")
        t = get()
        assert t["status"] == "ready", f"template import failed: {t.get('error')}\n{cluster.diagnostics()}"
        return importer, t

    try:
        importer, first = import_image("public.ecr.aws/docker/library/alpine:3.20")
        other = next(n for n, h in hosts.items() if h.host_id != importer)
        vm = make_vm(on=other, template_id=first["template_id"])
        assert host_of(cluster, vm) == other
        assert vm.commands.run("command -v python3").exit_code != 0
        # Re-import on the same host while `other` keeps its copy of the first
        # image: its next VM must boot the new image, not the stale copy.
        cluster.api.post(f"/v1/hosts/{hosts[other].host_id}/cordon")
        try:
            importer2, second = import_image("public.ecr.aws/docker/library/python:3.12-alpine")
        finally:
            cluster.api.post(f"/v1/hosts/{hosts[other].host_id}/uncordon")
        assert importer2 == importer and second["digest"] != first["digest"], (first, second)
        vm = make_vm(on=other, template_id=second["template_id"])
        r = vm.commands.run("python3 --version")
        assert r.exit_code == 0 and "3.12" in r.stdout, f"{r}\n{cluster.diagnostics()}"
    finally:
        cluster.api.delete(f"/templates/{name}")

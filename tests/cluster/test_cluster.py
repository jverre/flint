"""Cluster e2e: the flows in plans/03-cluster.md, against real hosts and VMs.

Run as root:  sudo -E .venv/bin/python -m pytest tests/cluster -v
"""

import json
import os
import ssl
import time
import urllib.error
import urllib.request

import httpx
import pytest
import websockets.sync.client as ws_sync

from flint import Sandbox

from .conftest import TENANT, host_of
from .harness import raw_join, wait_for, host_client_ctx


def state(sb: Sandbox) -> dict:
    return sb._fetch() or {}


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
    src.commands.run("echo before-fork > /root/state; (sleep 600 &) ; true")
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
    vm.commands.run("echo paused-state > /root/p; (sleep 700 &); true")
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
    for a, v in vms.items():
        v.write_file("/root/marker", a)
    wait_for(lambda: state(vms["durable"]).get("checkpoint_at"), 30, "the durable VM's first checkpoint")
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
    wait_for(lambda: state(durable).get("checkpoint_at"), 30, "the durable VM's first checkpoint")
    pid = state(durable)["pid"]
    hosts["b"].freeze()  # the agent hangs; snapd (the fence) keeps running
    t0 = time.time()
    killed_at = recovered_at = None
    while time.time() - t0 < 40 and not (killed_at and recovered_at):
        if killed_at is None and not os.path.exists(f"/proc/{pid}"):
            killed_at = time.time() - t0
        st = state(durable)
        if recovered_at is None and st.get("state") == "Started" and host_of(cluster, durable) == "a":
            recovered_at = time.time() - t0
        time.sleep(0.1)
    assert killed_at is not None, f"the fence never killed the durable VM\n{cluster.diagnostics()}"
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
    wait_for(lambda: state(durable).get("checkpoint_at"), 30, "the durable VM's first checkpoint")
    old_pid = state(durable)["pid"]
    for p in (hosts["b"].agent, hosts["b"].snapd):
        p.kill()
    assert os.path.exists(f"/proc/{old_pid}"), "the VM should outlive its agent"
    wait_for(lambda: state(durable).get("state") == "Started" and host_of(cluster, durable) == "a", 40,
             "the durable VM recovered on a")
    hosts["b"].ensure_up()
    assert not os.path.exists(f"/proc/{old_pid}"), "the lapsed copy on b is still running"
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
        v.commands.run(f"echo {v.id} > /root/id; (sleep 800 &); true")
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
    # VMs can't reach the control plane, host agents or snapd on the host.
    bridge_ip = f"{b.subnet}.1"
    for port in (cluster.port, cluster.port + 1, b.agent_port, b.snapd_port):
        r = mine.commands.run(f"nc -w2 {bridge_ip} {port} </dev/null")
        assert r.exit_code != 0, f"VM reached host port {port}"


@pytest.mark.slow
def test_template_built_on_one_host_boots_on_another(cluster, hosts, make_vm):
    r = cluster.api.post("/templates/build", json={"name": "cluster-e2e-tpl", "tenant": TENANT,
                                                   "dockerfile": "FROM alpine:3.20\nRUN echo built-in-template > /etc/tpl-marker\n"})
    assert r.status_code == 200, r.text
    tid, builder = r.json()["template_id"], r.json()["build_host"]
    try:
        wait_for(lambda: cluster.api.get(f"/templates/{tid}").json()["template"]["status"] == "ready", 900, "the template build")
        other = next(n for n, h in hosts.items() if h.host_id != builder)
        vm = make_vm(on=other, template_id=tid)
        assert host_of(cluster, vm) == other
        assert vm.read_file("/etc/tpl-marker") == b"built-in-template\n"
    finally:
        cluster.api.delete(f"/templates/{tid}")

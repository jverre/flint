"""Fixtures for the cluster e2e tests: one real two-host cluster per session."""

import os

import pytest

import flint
from flint import Sandbox

from .harness import Cluster, have_prereqs, wait_for

TENANT = "t-e2e"


@pytest.fixture(scope="session")
def cluster():
    reason = have_prereqs()
    if reason:
        pytest.skip(f"cluster tests not runnable here: {reason}")
    c = Cluster()
    try:
        c.start([("a", "zone-1"), ("b", "zone-2")])
    except Exception:
        diag = c.diagnostics()
        c.stop()
        pytest.fail(f"cluster failed to start:\n{diag}", pytrace=True)
    flint.configure(url=c.url, api_key=c.api.headers["Authorization"].split(" ", 1)[1], tenant=TENANT)
    yield c
    c.stop()


@pytest.fixture
def hosts(cluster):
    """Hosts a and b, ready and schedulable; restored after the test."""
    for h in cluster.hosts.values():
        h.ensure_up()
        cluster.api.post(f"/v1/hosts/{h.host_id}/uncordon")
        cluster.api.patch(f"/v1/hosts/{h.host_id}", json={"max_vms": 0})
    yield cluster.hosts
    for h in cluster.hosts.values():
        h.ensure_up()
        cluster.api.post(f"/v1/hosts/{h.host_id}/uncordon")
        cluster.api.patch(f"/v1/hosts/{h.host_id}", json={"max_vms": 0})


@pytest.fixture
def make_vm(cluster, hosts):
    """Create VMs (optionally pinned to a host by cordoning the others); kill them after."""
    made = []

    def make(on: str | None = None, **kwargs) -> Sandbox:
        others = [h for n, h in hosts.items() if on and n != on]
        for h in others:
            cluster.api.post(f"/v1/hosts/{h.host_id}/cordon")
        try:
            sb = Sandbox(**kwargs)
        finally:
            for h in others:
                cluster.api.post(f"/v1/hosts/{h.host_id}/uncordon")
        made.append(sb)
        return sb

    yield make
    for sb in made + getattr(make, "extra", []):
        sb.kill()


def host_of(cluster, sb: Sandbox) -> str:
    """Name of the host a VM is on according to the control plane."""
    hid = (sb._fetch() or {}).get("host_id")
    return next((n for n, h in cluster.hosts.items() if h.host_id == hid), hid or "")

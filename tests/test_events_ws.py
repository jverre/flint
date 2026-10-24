"""WebSocket test for /events: volume create/delete should round-trip."""

from __future__ import annotations

import json
import os
import threading
import time

import httpx

from .conftest import AUTH, BASE_URL, TEST_PORT, api, ws_connect


BASE = BASE_URL


def _collect(events: list, ws) -> None:
    try:
        for msg in ws:
            text = msg.decode() if isinstance(msg, bytes) else msg
            events.append(json.loads(text))
    except Exception:
        pass


def test_volume_events_broadcast(_ensure_daemon, daemon_only):
    ws = ws_connect("/events", open_timeout=5)
    events: list = []
    t = threading.Thread(target=_collect, args=(events, ws), daemon=True)
    t.start()

    # Give the subscriber a moment to register.
    time.sleep(0.2)

    name = "flint-evt-vol"
    # Best-effort pre-cleanup.
    for v in api.get(f"{BASE}/volumes").json().get("volumes", []):
        if v.get("name") == name:
            api.delete(f"{BASE}/volumes/{v['id']}")

    resp = api.post(f"{BASE}/volumes", json={"name": name, "size_gib": 1}, timeout=5.0)
    assert resp.status_code == 200
    vol_id = resp.json()["volume"]["id"]

    try:
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if any(e.get("type") == "volume.created" for e in events):
                break
            time.sleep(0.05)
        assert any(e.get("type") == "volume.created" for e in events), events
    finally:
        api.delete(f"{BASE}/volumes/{vol_id}")
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if any(e.get("type") == "volume.deleted" for e in events):
                break
            time.sleep(0.05)
        assert any(e.get("type") == "volume.deleted" for e in events), events
        ws.close()


def _host_dirs_of(vm_id: str) -> list[str]:
    """Directories on the host named after the VM: its jail or runtime directory."""
    from flint.core import config
    found = []
    for root in (config.DATA_DIR, config.JAILER_BASE_DIR):
        for base, dirs, _files in os.walk(root):
            found += [os.path.join(base, d) for d in dirs if vm_id in d]
            if base.count(os.sep) - root.count(os.sep) >= 1:
                dirs[:] = []  # jails are <root>/<vmm>/<id>; no need to go deeper
    return found


def test_deleting_a_vm_wipes_its_files(sandbox, daemon_only):
    """A deleted VM's files are wiped before removal, and vm.wiped says so."""
    events: list = []
    ws = ws_connect("/events", open_timeout=5)
    threading.Thread(target=_collect, args=(events, ws), daemon=True).start()
    try:
        r = sandbox.commands.run("head -c 8388608 /dev/urandom > /root/tenant-data && sync && echo ok", timeout=60)
        assert r.stdout.strip() == "ok", r.stderr
        assert _host_dirs_of(sandbox.id), "the test can't find the VM's directory on the host"
        api.delete(f"{BASE}/vms/{sandbox.id}").raise_for_status()

        deadline = time.monotonic() + 30
        wiped = None
        while time.monotonic() < deadline and wiped is None:
            wiped = next((e for e in events if e.get("type") == "vm.wiped" and e.get("vm_id") == sandbox.id), None)
            time.sleep(0.05)
        assert wiped, f"no vm.wiped event; got {[e.get('type') for e in events]}"
        assert wiped["files"] > 0 and wiped["bytes"] >= 8 << 20, wiped
        assert wiped["tenant"] == "default", wiped
        assert not _host_dirs_of(sandbox.id), _host_dirs_of(sandbox.id)
    finally:
        ws.close()

"""The audit log is a hash chain: changing or removing an entry shows."""

from __future__ import annotations

import os
import sqlite3
import time

from flint.daemon.eventlog import EventLog


def _log(tmp_path) -> tuple[EventLog, str]:
    db = os.path.join(tmp_path, "flint.db")
    log = EventLog(db)
    for i in range(5):
        log.audit(actor="token:abc", actor_name="ci", method="POST", path=f"/vms/{i}", status=200, tenant="acme")
    return log, db


def test_an_untouched_chain_verifies(tmp_path):
    log, _ = _log(tmp_path)
    assert log.verify_audit()["ok"] and log.verify_audit()["count"] == 5


def test_an_edited_entry_breaks_the_chain(tmp_path):
    log, db = _log(tmp_path)
    with sqlite3.connect(db) as c:
        c.execute("UPDATE audit SET actor = 'token:someone-else' WHERE seq = 3")
    r = log.verify_audit()
    assert (r["ok"], r["broken_at"]) == (False, 3), r


def test_a_removed_entry_breaks_the_chain(tmp_path):
    log, db = _log(tmp_path)
    with sqlite3.connect(db) as c:
        c.execute("DELETE FROM audit WHERE seq = 2")
    r = log.verify_audit()
    assert (r["ok"], r["broken_at"]) == (False, 3), r


def test_events_are_kept_per_tenant(tmp_path):
    log = EventLog(os.path.join(tmp_path, "flint.db"))
    for tenant in ("a", "b", "a"):
        log.record({"type": "vm.created", "ts": time.time(), "tenant": tenant, "vm": {"vm_id": f"vm-{tenant}"}, "actor": "socket"})
    assert [e["tenant"] for e in log.query(tenants=("a",))] == ["a", "a"]
    assert [e["seq"] for e in log.query(vm_id="vm-b")] == [2]
    assert [e["seq"] for e in log.query(since=2)] == [3]


def test_retention_preserves_the_last_link_and_sequence(tmp_path, monkeypatch):
    log, _ = _log(tmp_path)
    last = log.audit_entries()[-1]
    future = time.time() + 100 * 86400
    monkeypatch.setattr("flint.daemon.eventlog.time.time", lambda: future)
    log.record({"type": "vm.created", "ts": future})  # triggers retention
    assert log.audit_entries() == [last]
    entry = log.audit(actor="socket", actor_name="", method="POST", path="/vms", status=200)
    assert entry["seq"] == 6 and entry["prev_hash"] == last["hash"]
    assert log.verify_audit()["ok"]


def test_events_and_audit_survive_reopening(tmp_path):
    log, db = _log(tmp_path)
    log.record({"type": "vm.exec", "ts": time.time(), "tenant": "acme", "vm_id": "vm-1"})
    reopened = EventLog(db)
    assert reopened.query(tenants=("acme",))[0]["type"] == "vm.exec"
    assert reopened.verify_audit()["count"] == 5


def test_cli_reads_every_event_page(monkeypatch):
    from click.testing import CliRunner
    import flint.cli as cli

    cursors = []

    def page(method, path, params):
        cursors.append(params["since"])
        seq = params["since"] + 1
        return {"events": [{"seq": seq, "type": "vm.created"}] if seq <= 2 else [],
                "next": seq if seq <= 2 else params["since"]}

    monkeypatch.setattr(cli, "_cp", page)
    result = CliRunner().invoke(cli.cli, ["events", "-o", "json"])
    assert result.exit_code == 0, result.output
    assert cursors == [0, 1, 2]
    assert '"seq": 2' in result.output

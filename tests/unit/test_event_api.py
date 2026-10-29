"""Tenant authorization and audit attribution through the real HTTP middleware."""

from types import SimpleNamespace
import json
import socket
import threading
import time

from fastapi.testclient import TestClient

from flint.daemon.auth import ADMIN, SERVICE, TokenStore
from flint.daemon.eventlog import EventLog
from flint.daemon.server import app
from flint.daemon import events
from flint.core import _logs


def test_events_and_audit_obey_the_http_principal(tmp_path, monkeypatch):
    # TestClient builds and caches middleware; leave the shared app unstarted
    # for tests which install their own session middleware afterwards.
    monkeypatch.setattr(app, "middleware_stack", None)
    monkeypatch.setattr(events, "_bus", None)
    monkeypatch.setattr(_logs, "_bus", None)
    path = str(tmp_path / "flint.db")
    tokens, log = TokenStore(path), EventLog(path)
    _, admin = tokens.create(ADMIN, "ops")
    _, acme = tokens.create(SERVICE, "acme-backend", ["acme"])
    _, other = tokens.create(SERVICE, "other", ["other"])
    vm = {"vm_id": "vm-1", "tenant": "acme", "state": "dead"}

    def agent(vm_id):
        """A guest agent that answers one exec."""
        host, guest = socket.socketpair()
        body = json.dumps({"exit_code": 0, "stdout": "stored\n", "stderr": ""}).encode()

        def serve():
            with guest:
                guest.recv(65536)
                guest.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body))

        threading.Thread(target=serve, daemon=True).start()
        return host

    services = SimpleNamespace(tokens=tokens, eventlog=log, cluster_mode=False, start=lambda loop: None,
                               get_vm=lambda vm_id: vm if vm_id == "vm-1" else None, connect_agent=agent)
    monkeypatch.setattr(app.state, "services", services, raising=False)
    monkeypatch.setattr(events, "sink", log.record)
    monkeypatch.setattr(events, "vm_tenant", lambda vm_id: "acme")
    for tenant in ("", "other", "acme"):
        log.record({"type": "vm.deleted", "ts": time.time(), "tenant": tenant, "vm_id": "vm-1"})
    auth = lambda token: {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        page = client.get("/v1/events?limit=1", headers=auth(acme)).json()
        assert [e["tenant"] for e in page["events"]] == ["acme"]
        assert page["next"] == 3
        assert client.get("/v1/events?since=3", headers=auth(acme)).json()["events"] == []
        assert client.get("/vms/vm-1/timeline", headers=auth(other)).status_code == 404
        timeline = client.get("/vms/vm-1/timeline", headers=auth(acme))
        assert timeline.status_code == 200
        assert [e["tenant"] for e in timeline.json()["events"]] == ["acme"]
        result = client.post("/vms/vm-1/exec", headers=auth(acme), json={"cmd": ["sh", "-c", "echo stored"]})
        assert result.status_code == 200
        executed = client.get("/v1/events?type=vm.exec", headers=auth(acme)).json()["events"]
        assert len(executed) == 1
        assert (executed[0]["actor_name"], executed[0]["cmd"], executed[0]["stdout_tail"]) == (
            "acme-backend", "echo stored", "stored\n")
        assert client.get("/v1/audit", headers=auth(acme)).status_code == 403
        assert client.get("/v1/audit/verify", headers=auth(other)).status_code == 403
        made = client.post("/v1/tokens", headers=auth(admin), json={"kind": "service", "name": "worker"})
        assert made.status_code == 200, made.text
        entries = client.get("/v1/audit", headers=auth(admin)).json()["entries"]
        entry = next(e for e in entries if e["request_id"] == made.headers["x-request-id"])
        assert (entry["actor_name"], entry["path"], entry["status"]) == ("ops", "/v1/tokens", 200)
        assert "secret" not in str(entry)
        assert client.get("/v1/audit/verify", headers=auth(admin)).json()["ok"]

"""Unprivileged API-side adapters for the local Linux runtime."""
from __future__ import annotations

import asyncio
import base64
from contextlib import nullcontext
import http.client
import json
import re
import threading
from types import SimpleNamespace

from .runtime_protocol import RuntimeClient


class Record(SimpleNamespace):
    def to_dict(self):
        return vars(self).copy()


class Manager:
    def __init__(self, client):
        self.client = client

    def list_dicts(self):
        return self.client.request("vm.list")

    def get_dict(self, sandbox_id):
        if not re.fullmatch(r"[a-f0-9-]{36}", sandbox_id):
            return None
        return self.client.request("vm.get", sandbox_id=sandbox_id)

    def get_entry(self, sandbox_id):
        if not re.fullmatch(r"[a-f0-9-]{36}", sandbox_id):
            return None
        result = self.client.request("vm.entry", sandbox_id=sandbox_id)
        return Record(**result) if result else None

    def create(self, **kwargs):
        return self.client.request("vm.create", **{k: v for k, v in kwargs.items() if v is not None})

    def pause(self, sandbox_id):
        return self.client.request("vm.pause", sandbox_id=sandbox_id)

    def resume(self, sandbox_id):
        return self.client.request("vm.resume", sandbox_id=sandbox_id)

    def set_timeout(self, sandbox_id, timeout_seconds, policy="kill"):
        return self.client.request("vm.timeout", sandbox_id=sandbox_id, timeout_seconds=timeout_seconds, policy=policy)

    def update_network_policy(self, sandbox_id, policy):
        return self.client.request("vm.policy.set", sandbox_id=sandbox_id, policy=policy)

    def get_network_policy(self, sandbox_id):
        return self.client.request("vm.policy.get", sandbox_id=sandbox_id)

    def set_tenant_network(self, tenant, enabled):
        return self.client.request("tenant.network.set", tenant=tenant, enabled=enabled)

    def agent_request(self, sandbox_id, method, path, body=None, timeout=65):
        # The privileged worker connects only to this VM's fixed agent port.
        # All HTTP response parsing and guest-controlled bytes stay here.
        sock = self.client.request("vm.connect", sandbox_id=sandbox_id)
        sock.settimeout(timeout)
        conn = http.client.HTTPConnection("guest", timeout=timeout)
        conn.sock = sock
        try:
            conn.request(method, path, body, {"Content-Type": "application/json"} if body is not None else {})
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    async def bridge_terminal(self, sandbox_id, websocket):
        from websockets.asyncio.client import connect
        await websocket.accept()
        status, body = await asyncio.to_thread(self.agent_request, sandbox_id, "POST", "/processes",
            json.dumps({"cmd": ["/bin/sh", "-i"], "pty": True, "cols": 120, "rows": 40}).encode())
        if status != 201:
            await websocket.close(code=1011, reason="Failed to create PTY process")
            return
        pid = json.loads(body)["pid"]
        sock = await asyncio.to_thread(self.client.request, "vm.connect", sandbox_id=sandbox_id)
        sock.setblocking(False)
        try:
            async with connect(f"ws://guest/processes/{pid}/output", sock=sock, proxy=None) as guest:
                async def output():
                    async for message in guest:
                        event = json.loads(message)
                        if event.get("type") == "exit":
                            return
                        if event.get("type") in ("stdout", "stderr") and event.get("data"):
                            await websocket.send_bytes(base64.b64decode(event["data"]))
                async def input_():
                    while True:
                        data = await websocket.receive_bytes()
                        await asyncio.to_thread(self.agent_request, sandbox_id, "POST", f"/processes/{pid}/input", data, 5)
                tasks = [asyncio.create_task(output()), asyncio.create_task(input_())]
                try:
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            sock.close()
            await asyncio.to_thread(self.agent_request, sandbox_id, "POST", f"/processes/{pid}/signal",
                                    json.dumps({"signal": 9}).encode(), 5)


class Volumes:
    def __init__(self, client):
        self.client = client

    def list(self, tenants=None):
        return [Record(**v) for v in self.client.request("volume.list", tenants=tenants)]

    def _require(self, ref, tenants=None):
        return Record(**self.client.request("volume.get", ref=ref, tenants=tenants))

    def create(self, name, size_gib=None, tenant="default"):
        return Record(**self.client.request("volume.create", name=name, size_gib=size_gib, tenant=tenant))

    def snapshot(self, ref, name=None, tenants=None):
        return Record(**self.client.request("volume.snapshot", ref=ref, name=name, tenants=tenants))

    def fork(self, ref, name=None, tenants=None):
        return Record(**self.client.request("volume.fork", ref=ref, name=name, tenants=tenants))

    def delete(self, ref, tenants=None):
        return Record(**self.client.request("volume.delete", ref=ref, tenants=tenants))

    def list_mounts(self, vm_id):
        return [Record(**m) for m in self.client.request("volume.mounts", vm_id=vm_id)]

    def mount(self, vm_id, volume, path, subpath="", readonly=False, exclusive=False):
        value = self.client.request("volume.mount", vm_id=vm_id, volume=volume, path=path,
                                    subpath=subpath, readonly=readonly, exclusive=exclusive)
        return Record(**value)

    def unmount(self, vm_id, path):
        return Record(**self.client.request("volume.unmount", vm_id=vm_id, path=path))


class EventLog:
    def __init__(self, client):
        self.client = client

    def record(self, event):
        return self.client.request("events.record", event=event)

    def query(self, **kwargs):
        return self.client.request("events.query", **kwargs)

    def audit(self, **kwargs):
        return self.client.request("audit.append", **kwargs)

    def audit_entries(self, since=0, limit=500):
        return self.client.request("audit.query", since=since, limit=limit)

    def verify_audit(self):
        return self.client.request("audit.verify")


class Quotas:
    def __init__(self, client):
        self.client = client

    def reserve(self, *args):
        # Atomic admission and accounting belong to the worker. Keeping the
        # reservation here would trust a compromised API to enforce its limits.
        return nullcontext()

    def set(self, tenant, changes):
        return self.client.request("quota.set", tenant=tenant, changes=changes)

    def view(self, tenant, _used):
        return self.client.request("quota.view", tenant=tenant)


class Frontend:
    def __init__(self, client: RuntimeClient):
        from flint.core import config
        from .auth import TokenStore
        self.runtime = client
        info = client.request("info")
        self.backend = SimpleNamespace(**info["backend"], vm_resources=lambda: tuple(info["resources"]))
        self.started_at, self.golden_ready = info["started_at"], info["golden_ready"]
        self.manager, self.volumes = Manager(client), Volumes(client)
        self.eventlog, self.quotas = EventLog(client), Quotas(client)
        self.tokens = TokenStore(config.DAEMON_DB_PATH)
        self.tokens.ensure_admin_token_file()
        with open(config.ADMIN_TOKEN_PATH) as stream:
            client.request("auth.admin.publish", token=stream.read().strip())
        self.cluster = None
        self.metrics = SimpleNamespace(start=lambda loop: None,
            get=lambda vm_id, window=60: client.request("metrics.get", vm_id=vm_id, window=window))
        self._state_store = SimpleNamespace(tenant_network=lambda tenant: client.request("tenant.network.get", tenant=tenant))
        self.storage = SimpleNamespace(kind=info["storage_kind"],
            is_running=lambda: client.request("info")["storage_healthy"],
            setup_sandbox=lambda **kw: client.request("storage.setup", vm_id=kw["vm_id"])) if info["storage_kind"] else None

    def kill_vm(self, vm_id):
        return self.runtime.request("vm.kill", vm_id=vm_id)

    def notifications(self):
        from . import events
        from flint.core import _logs
        cursor = 0
        while True:
            try:
                for item in self.runtime.request("notify.poll", since=cursor):
                    cursor = item["cursor"]
                    if item["kind"] == "event" and events.get_bus():
                        events.get_bus().publish(item["value"])
                    elif item["kind"] == "log" and _logs.get_bus():
                        data = item["value"]
                        _logs.get_bus().append(data["vm_id"], data["line"])
            except (OSError, RuntimeError):
                # The API cannot continue safely if its supervisor is gone.
                import os
                os._exit(1)


def main():
    import os
    import sys
    import uvicorn
    from . import server, events
    if os.geteuid() == 0 or os.getgroups():
        raise SystemExit("The public API must run unprivileged without supplementary groups")
    client = RuntimeClient(sys.argv[1])
    frontend = Frontend(client)
    server.app.state.daemon = frontend
    events.sink = frontend.eventlog.record
    events.vm_tenant = lambda vm_id: (frontend.manager.get_dict(vm_id) or {}).get("tenant", "")
    server.FlintDaemon._init_sso(frontend)
    # Hooks start after ASGI initializes its event and log buses.
    @server.app.on_event("startup")
    async def notifications():
        threading.Thread(target=frontend.notifications, daemon=True, name="runtime-events").start()
    sockets = [client.request("listen", index=i) for i in (0, 1)]
    uvicorn.Server(uvicorn.Config(server.app, log_level="warning", timeout_graceful_shutdown=5)).run(sockets=sockets)


if __name__ == "__main__":
    main()

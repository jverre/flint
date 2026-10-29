"""The Linux daemon's public API, run unprivileged (started by ``supervisor.py``).

It serves the same app as an in-process daemon, with :class:`RemoteServices`:
each service call goes over the runtime socket to the privileged runtime's
``LocalServices`` (``runtime.py``). It owns authentication (tokens, SSO
sessions) itself and parses everything guests send.
"""
from __future__ import annotations

import inspect
import logging
import os
import re
import threading
import time

from .runtime_protocol import RuntimeClient
from .services import LocalServices, operations

log = logging.getLogger("flint.daemon")
_VM_ID = re.compile(r"[a-f0-9-]{36}")


class RemoteEventLog:
    """The runtime's stored events and audit log (``eventlog.EventLog``'s interface)."""

    def __init__(self, client: RuntimeClient) -> None:
        self._client = client

    def record(self, event):
        return self._client.request("events.record", event=event)

    def query(self, **filters):
        # "" means no VM filter, and no VM has a non-canonical ID.
        vm_id = filters.pop("vm_id", "")
        if vm_id and not _VM_ID.fullmatch(vm_id):
            return []
        return self._client.request("events.query", **filters, **({"vm_id": vm_id} if vm_id else {}))

    def audit(self, **entry):
        return self._client.request("audit.append", **entry)

    def audit_entries(self, since=0, limit=500):
        return self._client.request("audit.query", since=since, limit=limit)

    def verify_audit(self):
        return self._client.request("audit.verify")


def _forward(name: str):
    signature = inspect.signature(getattr(LocalServices, name))

    def call(self, *args, **kwargs):
        from flint.core.policy import as_json
        arguments = signature.bind(self, *args, **kwargs).arguments
        return self._client.request(name, **{k: as_json(v) for k, v in arguments.items() if k != "self"})

    call.__name__ = call.__qualname__ = name
    call.__doc__ = getattr(LocalServices, name).__doc__
    return call


class RemoteServices:
    """:class:`~flint.daemon.services.Services` served by the privileged runtime."""

    cluster_mode = False  # cluster host agents serve their API in-process

    def __init__(self, client: RuntimeClient, tokens) -> None:
        self._client = client
        self.tokens = tokens
        self.eventlog = RemoteEventLog(client)

    def start(self, loop) -> None:
        # The runtime samples metrics; this process relays its events and log lines.
        threading.Thread(target=self._relay_notifications, daemon=True, name="runtime-events").start()

    def get_vm(self, vm_id: str):
        return self._client.request("get_vm", vm_id=vm_id) if _VM_ID.fullmatch(vm_id) else None

    def _relay_notifications(self) -> None:
        """Publish the runtime's events and log lines on this process's buses (it stored the events)."""
        from flint.core import _logs
        from . import events
        cursor = 0
        while True:
            try:
                for item in self._client.request("notify.poll", since=cursor):
                    cursor = item["cursor"]
                    if item["kind"] == "event" and events.get_bus():
                        events.get_bus().publish(item["value"])
                    elif item["kind"] == "log" and _logs.get_bus():
                        data = item["value"]
                        _logs.get_bus().append(data["vm_id"], data["line"])
            except OSError:
                # The API cannot continue safely if its supervisor is gone.
                os._exit(1)
            except Exception:
                # A busy runtime or a bad page must not end event delivery.
                log.exception("Runtime notification poll failed; retrying")
                time.sleep(1)


for _name in operations(LocalServices):
    if _name not in vars(RemoteServices):
        setattr(RemoteServices, _name, _forward(_name))


def main():
    import sys
    import uvicorn
    from flint.core import config
    from . import events, server
    from .auth import TokenStore
    from .lifecycle import configure_logging
    if os.geteuid() == 0 or os.getgroups():
        raise SystemExit("The public API must run unprivileged without supplementary groups")
    configure_logging()
    client = RuntimeClient(sys.argv[1])
    tokens = TokenStore(config.DAEMON_DB_PATH)
    tokens.ensure_admin_token_file()
    with open(config.ADMIN_TOKEN_PATH) as stream:
        client.request("auth.admin.publish", token=stream.read().strip())
    services = RemoteServices(client, tokens)
    server.app.state.services = services
    events.sink = services.eventlog.record
    events.vm_tenant = lambda vm_id: (services.get_vm(vm_id) or {}).get("tenant", "")
    server.enable_sso()
    sockets = [client.request("listen", index=i) for i in (0, 1)]
    uvicorn.Server(uvicorn.Config(server.app, log_level="warning", timeout_graceful_shutdown=5)).run(sockets=sockets)


if __name__ == "__main__":
    main()

"""Privileged Linux runtime. Only resource operations cross this boundary.

The public HTTP server runs under a different UID in a separate filesystem
and calls the :class:`~flint.daemon.services.LocalServices` here over a Unix
socket. This socket is not a general Python object proxy: it serves the
methods marked :func:`~flint.daemon.services.operation` and a few of its
own, arguments must match their signature, and host paths come from runtime
state rather than the caller. Guest connections are passed as FDs so HTTP,
WebSocket and file payloads are parsed by the unprivileged frontend.

Errors cross as ``{"status", "detail", "headers"}``; the frontend raises the
matching :mod:`flint.core.errors` class (see ``runtime_protocol.py``).
"""
from __future__ import annotations

from collections import deque
import inspect
import os
import re
import socket
import socketserver
import struct
import threading

from flint.core import _logs, config
from flint.core.errors import FlintError, SandboxNotFound
from . import events
from .runtime_protocol import MAX_RESPONSE, receive, send
from .services import LocalServices, operations

_VM_ID = re.compile(r"[a-f0-9-]{36}")


class Runtime:
    def __init__(self, daemon, uid: int, sockets: list[socket.socket]) -> None:
        self.daemon, self.uid, self.sockets = daemon, uid, sockets
        daemon.api_uid = uid
        self._condition = threading.Condition()
        self._notifications = deque(maxlen=8192)
        self._sequence = 0
        services, e = daemon.services, daemon.eventlog
        self.operations = {name: getattr(services, name) for name in operations(LocalServices)}
        self.operations.update({
            "listen": self.listen, "notify.poll": self.poll, "auth.admin.publish": self.publish_admin_token,
            "events.query": e.query, "events.record": e.record,
            "audit.append": e.audit, "audit.query": e.audit_entries, "audit.verify": e.verify_audit,
        })
        events.hooks.append(lambda event: self.notify("event", event))
        _logs.hooks.append(lambda vm_id, line: self.notify("log", {"vm_id": vm_id, "line": line}))

    def dispatch(self, request: dict):
        if set(request) != {"operation", "arguments", "actor"}:
            raise ValueError("Invalid runtime request fields")
        name, args, attribution = request["operation"], request["arguments"], request["actor"]
        if not isinstance(name, str) or name not in self.operations or not isinstance(args, dict):
            raise ValueError("Unknown runtime operation")
        if not isinstance(attribution, list) or len(attribution) != 2 or any(not isinstance(s, str) or len(s) > 512 for s in attribution):
            raise ValueError("Invalid actor")
        if "vm_id" in args and (not isinstance(args["vm_id"], str) or not _VM_ID.fullmatch(args["vm_id"])):
            raise SandboxNotFound.of(str(args["vm_id"])[:64])  # no VM has another form of ID
        function = self.operations[name]
        # Reject extra fields, including attempted host paths or command names.
        inspect.signature(function).bind(**args)
        from .eventlog import actor
        token = actor.set(tuple(attribution))
        try:
            return function(**args)
        finally:
            actor.reset(token)

    def listen(self, index: int):
        if type(index) is not int or index not in (0, 1):
            raise ValueError("Invalid listener")
        return self.sockets[index].dup()

    def notify(self, kind, value):
        with self._condition:
            self._sequence += 1
            self._notifications.append({"cursor": self._sequence, "kind": kind, "value": value})
            self._condition.notify_all()

    def poll(self, since: int):
        if type(since) is not int or since < 0:
            raise ValueError("Invalid notification cursor")
        with self._condition:
            if since >= self._sequence:
                self._condition.wait(1)
            return [n for n in self._notifications if n["cursor"] > since][:256]

    def publish_admin_token(self, token: str):
        # Authentication state belongs to the frontend. Publish only its
        # current admin token to the existing root-owned CLI discovery path;
        # never open an API-owned SQLite database in the root supervisor.
        if not isinstance(token, str) or not re.fullmatch(r"flint_[0-9a-f]{12}_[A-Za-z0-9-]{16,128}", token):
            raise ValueError("Invalid admin token format")
        path = config.ADMIN_TOKEN_PATH + ".tmp"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(token + "\n")
        os.replace(path, config.ADMIN_TOKEN_PATH)


def wire_error(exc: BaseException) -> dict:
    """How an operation's failure crosses to the API: its status, detail and headers."""
    from .auth import AuthError
    if isinstance(exc, FlintError):
        return {"status": exc.status, "detail": exc.detail, "headers": exc.headers}
    if isinstance(exc, AuthError):
        return {"status": exc.status, "detail": exc.detail, "headers": None}
    if isinstance(exc, (TypeError, ValueError)):
        return {"status": 400, "detail": str(exc), "headers": None}
    config.log.error("runtime operation failed", exc_info=exc)
    return {"status": None, "detail": "Internal error in the Flint runtime; see its log", "headers": None}


class RuntimeServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    block_on_close = False
    # socketserver's default backlog of 5 refuses API bursts with EAGAIN.
    request_queue_size = 1024

    def __init__(self, path: str, runtime: Runtime):
        self.runtime = runtime
        self._slots = threading.BoundedSemaphore(64)
        super().__init__(path, _Handler)
        os.chown(path, runtime.uid, -1)
        os.chmod(path, 0o600)


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        conn, runtime = self.request, self.server.runtime
        _pid, uid, _gid = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid != runtime.uid:
            return
        if not self.server._slots.acquire(blocking=False):
            try:
                send(conn, {"error": {"detail": "The runtime is busy; retry later", "status": 503,
                                      "headers": {"Retry-After": "1"}}})
            except OSError:
                pass
            return
        try:
            conn.settimeout(5)
            request, incoming_fd = receive(conn)
            if incoming_fd is not None:
                os.close(incoming_fd)
                raise ValueError("Runtime requests cannot supply file descriptors")
            if request.get("operation") == "listen":
                runtime.daemon.api_pid = _pid
            try:
                value = runtime.dispatch(request)
                if isinstance(value, socket.socket):
                    with value:
                        send(conn, {"value": None}, value.fileno())
                else:
                    try:
                        send(conn, {"value": value}, limit=MAX_RESPONSE)
                    except ValueError as exc:  # raised before anything is written
                        raise FlintError("Response too large; request fewer items") from exc
            except Exception as exc:
                send(conn, {"error": wire_error(exc)})
        except (OSError, ValueError):
            pass
        finally:
            self.server._slots.release()

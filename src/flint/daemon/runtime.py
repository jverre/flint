"""Privileged Linux runtime. Only resource operations cross this boundary.

The public HTTP server runs under a different UID in a separate filesystem.
This socket is not a general Python object proxy: every operation is listed
here, arguments must match its signature, and host paths come from runtime
state rather than the caller. Guest connections are passed as FDs so HTTP,
WebSocket and file payloads are parsed by the unprivileged frontend.
"""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, is_dataclass
import inspect
import os
import re
import socket
import socketserver
import struct
import threading

from flint.core import config
from flint.core._netns import _enter_netns, _restore_netns
from flint.core._quotas import Usage
from . import events
from .runtime_protocol import receive, send


class Runtime:
    def __init__(self, daemon, uid: int, sockets: list[socket.socket]) -> None:
        self.daemon, self.uid, self.sockets = daemon, uid, sockets
        daemon._api_uid = uid
        self._condition = threading.Condition()
        self._notifications = deque(maxlen=8192)
        self._sequence = 0
        self._build_lock = threading.RLock()
        self._builds: dict[str, threading.Thread] = {}
        m, v, e, q = daemon.manager, daemon.volumes, daemon.eventlog, daemon.quotas
        self.operations = {
            "info": self.info, "listen": self.listen, "notify.poll": self.poll,
            "vm.list": m.list_dicts, "vm.get": m.get_dict, "vm.entry": self.entry,
            "vm.create": self.create, "vm.kill": daemon.kill_vm, "vm.pause": m.pause,
            "vm.resume": m.resume, "vm.timeout": m.set_timeout,
            "vm.policy.get": m.get_network_policy, "vm.policy.set": m.update_network_policy,
            "vm.connect": self.connect, "tenant.network.get": daemon._state_store.tenant_network,
            "tenant.network.set": m.set_tenant_network,
            "volume.list": v.list, "volume.get": v._require, "volume.create": self.create_volume,
            "volume.snapshot": self.snapshot_volume, "volume.fork": self.fork_volume,
            "volume.delete": v.delete, "volume.mount": v.mount, "volume.unmount": v.unmount,
            "volume.mounts": v.list_mounts, "storage.setup": self.setup_storage,
            "quota.set": q.set, "quota.view": self.quota_view,
            "events.query": e.query, "events.record": e.record,
            "audit.append": e.audit, "audit.query": e.audit_entries, "audit.verify": e.verify_audit,
            "template.list": self.templates, "template.get": self.template,
            "template.build": self.build, "template.delete": self.delete_template,
            "config.get": self.get_config, "config.set": self.set_config,
            "auth.admin.publish": self.publish_admin_token,
            "state.write": self.write_state, "metrics.get": daemon.metrics.get,
        }
        events.hooks.append(lambda event: self.notify("event", event))
        from flint.core import _logs
        _logs.hooks.append(lambda vm_id, line: self.notify("log", {"vm_id": vm_id, "line": line}))

    def dispatch(self, request: dict):
        if set(request) != {"operation", "arguments", "actor"}:
            raise ValueError("Invalid runtime request fields")
        name, args, attribution = request["operation"], request["arguments"], request["actor"]
        if not isinstance(name, str) or name not in self.operations or not isinstance(args, dict):
            raise ValueError("Unknown runtime operation")
        if not isinstance(attribution, list) or len(attribution) != 2 or any(not isinstance(s, str) or len(s) > 512 for s in attribution):
            raise ValueError("Invalid actor")
        for key in ("vm_id", "sandbox_id"):
            if key in args and (not isinstance(args[key], str) or not re.fullmatch(r"[a-f0-9-]{36}", args[key])):
                raise ValueError("Canonical VM ID required")
        function = self.operations[name]
        # Reject extra fields, including attempted host paths or command names.
        inspect.signature(function).bind(**args)
        from .eventlog import actor
        token = actor.set(tuple(attribution))
        try:
            return function(**args)
        finally:
            actor.reset(token)

    def info(self):
        from flint.core import _nfs_server
        d = self.daemon
        return {"backend": {"name": d.backend.name, "kind": d.backend.kind,
                             "supports_network_policy": d.backend.supports_network_policy},
                "resources": list(d.backend.vm_resources()), "started_at": d.started_at,
                "golden_ready": d.golden_ready, "storage_kind": d.storage.kind if d.storage else "",
                "storage_healthy": d.storage.is_running() if d.storage else False,
                "runtime_pid": os.getpid(), "api_uid": self.uid,
                "image_volumes": _nfs_server.images_available()}

    def listen(self, index: int):
        if type(index) is not int or index not in (0, 1):
            raise ValueError("Invalid listener")
        return self.sockets[index].dup()

    def entry(self, sandbox_id: str):
        entry = self.daemon.manager.get_entry(sandbox_id)
        return {"vm_id": entry.vm_id, "backend_metadata": entry.backend_metadata,
                "ns_name": "", "agent_url": ""} if entry else None

    def connect(self, sandbox_id: str):
        entry = self.daemon.manager.get_entry(sandbox_id)
        if entry is None:
            raise ValueError("VM not found")
        old = _enter_netns(entry.ns_name)
        try:
            return socket.create_connection((config.GUEST_IP, config.AGENT_PORT), timeout=30)
        finally:
            _restore_netns(old)

    def usage(self, tenant: str):
        vms = sum(v.get("tenant") == tenant for v in self.daemon.manager.list_dicts())
        cpu, memory = self.daemon.backend.vm_resources()
        disk = sum(v.size_gib or 0 for v in self.daemon.volumes.list((tenant,)))
        return Usage(vms, vms * cpu, vms * memory, disk)

    def create(self, *, template_id: str = "default", allow_internet_access: bool = True,
               use_pool: bool = True, use_pyroute2: bool = True, network_policy: dict | None = None,
               vm_id=None, tenant: str = "default", availability: str = ""):
        if vm_id is not None or availability:
            raise ValueError("Cluster parameters are not accepted by the local runtime")
        from .auth import TRUSTED
        from flint.core._template_registry import _slugify
        tenant = TRUSTED.resolve_tenant(tenant)
        if not isinstance(template_id, str) or _slugify(template_id) != template_id:
            raise ValueError("Invalid template ID")
        if any(type(v) is not bool for v in (allow_internet_access, use_pool, use_pyroute2)):
            raise ValueError("VM flags must be booleans")
        cpu, memory = self.daemon.backend.vm_resources()
        with self.daemon.quotas.reserve(tenant, lambda: self.usage(tenant), Usage(1, cpu, memory)):
            return self.daemon.manager.create(template_id=template_id, tenant=tenant,
                allow_internet_access=allow_internet_access, use_pool=use_pool,
                use_pyroute2=use_pyroute2, network_policy=network_policy)

    def setup_storage(self, vm_id: str):
        entry = self.daemon.manager.get_entry(vm_id)
        if entry and self.daemon.storage:
            self.daemon.storage.setup_sandbox(vm_id=vm_id, template_id=entry.template_id,
                veth_ip=entry.backend_metadata.get("veth_ip", ""), ns_name=entry.ns_name,
                agent_url=entry.agent_url)

    def create_volume(self, name, size_gib=None, tenant="default"):
        from flint.core import _nfs_server
        from flint.core._volumes import DEFAULT_SIZE_GIB
        from .auth import TRUSTED
        tenant = TRUSTED.resolve_tenant(tenant)
        size = size_gib or DEFAULT_SIZE_GIB if _nfs_server.images_available() else 0
        if type(size) is not int or size < 0:
            raise ValueError("Invalid volume size")
        with self.daemon.quotas.reserve(tenant, lambda: self.usage(tenant), Usage(volume_gib=size)):
            return self.daemon.volumes.create(name, size_gib, tenant)

    def copy_volume(self, method, ref, name, tenants):
        src = self.daemon.volumes._require(ref, tenants)
        with self.daemon.quotas.reserve(src.tenant, lambda: self.usage(src.tenant), Usage(volume_gib=src.size_gib or 0)):
            return method(ref, name, tenants)

    def snapshot_volume(self, ref, name=None, tenants=None):
        return self.copy_volume(self.daemon.volumes.snapshot, ref, name, tenants)

    def fork_volume(self, ref, name=None, tenants=None):
        return self.copy_volume(self.daemon.volumes.fork, ref, name, tenants)

    def quota_view(self, tenant: str):
        return self.daemon.quotas.view(tenant, self.usage(tenant))

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

    def templates(self):
        from flint.core._template_registry import list_templates
        return list_templates()

    def template(self, template_id: str):
        from flint.core._template_registry import get_template
        return get_template(template_id)

    def build(self, name: str, dockerfile: str, rootfs_size_mb: int, tenant: str):
        from fastapi import HTTPException
        from flint.core._template_registry import _slugify
        from flint.core._template_registry import claim_template, update_template_artifact_status
        from .auth import TRUSTED
        tenant = TRUSTED.resolve_tenant(tenant)
        if not isinstance(name, str) or not name or not isinstance(dockerfile, str) or not dockerfile:
            raise ValueError("name and dockerfile are required")
        if type(rootfs_size_mb) is not int or not 64 <= rootfs_size_mb <= 16384:
            raise ValueError("Invalid rootfs size")
        template_id = _slugify(name)
        if not template_id or template_id == "default":
            raise ValueError("Invalid template name")
        with self._build_lock:
            if template_id in self._builds and self._builds[template_id].is_alive():
                raise HTTPException(409, "A build for this template is already running")
            old = self.template(template_id)
            if old and old.get("tenant", "") != tenant:
                raise HTTPException(403, "Template belongs to another tenant")
            from flint.core._build_job import Admission, BuildBusy
            try:
                admission = Admission()
            except BuildBusy as exc:
                raise HTTPException(429, str(exc), headers={"Retry-After": "5"}) from exc
            try:
                claim_template(template_id, name, tenant)
                update_template_artifact_status(template_id, self.daemon.backend.kind, "building")
            except Exception:
                admission.release()
                raise
            def run():
                try:
                    with admission:
                        self.daemon.backend.build_template(name, dockerfile, rootfs_size_mb=rootfs_size_mb)
                except Exception as exc:
                    update_template_artifact_status(template_id, self.daemon.backend.kind, "failed", error=str(exc))
                    config.log.exception("Template build failed: %s", template_id)
                finally:
                    with self._build_lock:
                        self._builds.pop(template_id, None)
            thread = threading.Thread(target=run, daemon=True, name=f"build-{template_id}")
            self._builds[template_id] = thread
            try:
                thread.start()
            except Exception:
                admission.release()
                self._builds.pop(template_id, None)
                raise
        return {"template_id": template_id, "status": "building"}

    def delete_template(self, template_id: str):
        import shutil
        from fastapi import HTTPException
        from flint.core._template_registry import delete_template
        if template_id == "default":
            raise ValueError("Cannot delete default template")
        with self._build_lock:
            if template_id in self._builds and self._builds[template_id].is_alive():
                raise HTTPException(409, "Cannot delete a template while its build is running")
            template = self.template(template_id)
            if template is None:
                raise HTTPException(404, "Template not found")
            self.daemon.backend.delete_template_artifact(template_id, template)
            for artifact in (template.get("artifacts") or {}).values():
                directory = artifact.get("template_dir")
                if directory and os.path.isdir(directory):
                    shutil.rmtree(directory)
            delete_template(template_id)
        return {"ok": True}

    def get_config(self):
        from .server import get_config
        return get_config()

    def set_config(self, body: dict):
        from .server import patch_config
        return patch_config(body)

    def write_state(self):
        from .server import _write_state
        _write_state(self.daemon)

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


def _json_value(value):
    if is_dataclass(value):
        return value.to_dict() if hasattr(value, "to_dict") else asdict(value)
    if isinstance(value, list):
        return [_json_value(v) for v in value]
    return value


class RuntimeServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    block_on_close = False

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
        if uid != runtime.uid or not self.server._slots.acquire(blocking=False):
            return
        try:
            conn.settimeout(5)
            request, incoming_fd = receive(conn)
            if incoming_fd is not None:
                os.close(incoming_fd)
                raise ValueError("Runtime requests cannot supply file descriptors")
            if request.get("operation") == "listen":
                runtime.daemon._api_pid = _pid
            try:
                value = runtime.dispatch(request)
                if isinstance(value, socket.socket):
                    with value:
                        send(conn, {"value": None}, value.fileno())
                else:
                    send(conn, {"value": _json_value(value)})
            except Exception as exc:
                status = getattr(exc, "status_code", getattr(exc, "status", None))
                detail = getattr(exc, "detail", str(exc))
                if isinstance(exc, (TypeError, ValueError)):
                    status = 400
                send(conn, {"error": {"kind": type(exc).__name__, "detail": detail, "status": status,
                                      "headers": getattr(exc, "headers", None)}})
        except (OSError, ValueError):
            pass
        finally:
            self.server._slots.release()

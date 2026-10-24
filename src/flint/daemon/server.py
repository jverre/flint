from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import threading
import time
import urllib.parse

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
import uvicorn

from flint.core.backends import get_host_backend
from flint.core.config import (
    log, DAEMON_HOST, DAEMON_PORT, DAEMON_DIR, DAEMON_STATE_PATH, DAEMON_PID_PATH,
    TEMPLATES_DIR, DEFAULT_TEMPLATE_ID, DAEMON_SOCKET, ADMIN_TOKEN_PATH,
    DAEMON_DB_PATH, HEALTH_CHECK_INTERVAL, ERROR_CLEANUP_DELAY,
    WORKSPACE_DIR,
)
from flint.core.manager import SandboxManager
from flint.core.types import SandboxState
from flint.core._template_registry import (
    list_templates, get_template, delete_template as _delete_template,
)
from flint.core._volumes import DEFAULT_SIZE_GIB, Volumes, VolumeError
from flint.core._quotas import QuotaExceeded, Quotas, Usage
from flint.core import _nfs_server
from flint.core._metrics import MetricsSampler
from flint.core import _logs
from flint.daemon import events as _events
from flint.daemon.auth import AuthError, AuthMiddleware, Principal, TokenStore, principal_of
from flint.daemon import sso as _sso

app = FastAPI()
app.include_router(_sso.router)


def _vm_tenant(vm_id: str) -> str | None:
    daemon = getattr(app.state, "daemon", None)
    vm = daemon.manager.get_dict(vm_id) if daemon and daemon.manager else None
    return vm.get("tenant", "") if vm else None


app.add_middleware(
    AuthMiddleware,
    tokens=lambda: getattr(getattr(app.state, "daemon", None), "tokens", None),
    cluster_mode=lambda: bool(getattr(getattr(app.state, "daemon", None), "cluster", None)),
    vm_tenant=_vm_tenant,
    eventlog=lambda: getattr(getattr(app.state, "daemon", None), "eventlog", None),
)


@app.exception_handler(AuthError)
async def _auth_error(_request: Request, e: AuthError):
    from fastapi.responses import JSONResponse
    return JSONResponse({"detail": e.detail}, status_code=e.status)


@app.exception_handler(QuotaExceeded)
async def _quota_exceeded(_request: Request, e: QuotaExceeded):
    from fastapi.responses import JSONResponse
    return JSONResponse({"detail": str(e)}, status_code=429)


@app.on_event("startup")
async def _startup_event_bus():
    loop = asyncio.get_running_loop()
    _events.init_bus(loop)
    _logs.init_bus(loop)
    daemon = getattr(app.state, "daemon", None)
    if daemon is not None and daemon.metrics is not None:
        daemon.metrics.start(loop)


# ── Helpers ────────────────────────────────────────────────────────────────

def _get_daemon() -> FlintDaemon:
    daemon = getattr(app.state, "daemon", None)
    if daemon is None:
        raise HTTPException(status_code=503, detail="Daemon not initialized")
    return daemon


def _require_manager() -> SandboxManager:
    daemon = _get_daemon()
    if daemon.manager is None:
        raise HTTPException(status_code=503, detail="Manager not initialized")
    return daemon.manager


def _usage(tenant: str) -> Usage:
    """What a tenant holds on this host: its VMs (paused too) and volumes."""
    daemon = _get_daemon()
    vms = sum(1 for d in daemon.manager.list_dicts() if d.get("tenant") == tenant) if daemon.manager else 0
    vcpus, mem = daemon.backend.vm_resources()
    gib = sum(v.size_gib or 0 for v in daemon.volumes.list((tenant,))) if daemon.volumes else 0
    return Usage(vms, vms * vcpus, vms * mem, gib)


def _one_vm() -> Usage:
    vcpus, mem = _get_daemon().backend.vm_resources()
    return Usage(1, vcpus, mem, 0)


def _template_for(principal: Principal, template_id: str) -> dict | None:
    """A template the caller can see: shared (no tenant) or one of their tenants'."""
    tmpl = get_template(template_id)
    if tmpl is None and template_id == DEFAULT_TEMPLATE_ID:
        return {"template_id": template_id}
    if tmpl is None or (tmpl.get("tenant") and not principal.can_tenant(tmpl["tenant"])):
        return None
    return tmpl


def _require_entry(vm_id: str):
    """Get a sandbox entry or raise 404."""
    mgr = _require_manager()
    entry = mgr.get_entry(vm_id)
    if not entry:
        raise HTTPException(status_code=404, detail="VM not found")
    return entry


def _validate_network_policy(policy: object) -> None:
    """Basic schema validation for a network policy dict. Raises HTTPException on invalid input."""
    if not isinstance(policy, dict):
        raise HTTPException(status_code=400, detail="Network policy must be a JSON object")
    allow = policy.get("allow")
    if allow is not None:
        if not isinstance(allow, dict):
            raise HTTPException(status_code=400, detail="'allow' must be a JSON object mapping domains to rule lists")
        for domain, rules in allow.items():
            if not isinstance(domain, str):
                raise HTTPException(status_code=400, detail=f"Domain key must be a string, got {type(domain).__name__}")
            if not isinstance(rules, list):
                raise HTTPException(status_code=400, detail=f"Rules for domain '{domain}' must be a list")
            for rule in rules:
                if not isinstance(rule, dict):
                    raise HTTPException(status_code=400, detail=f"Each rule for domain '{domain}' must be a JSON object")
                for transform in rule.get("transform", []):
                    if not isinstance(transform, dict):
                        raise HTTPException(status_code=400, detail=f"Each transform for domain '{domain}' must be a JSON object")
                    headers = transform.get("headers")
                    if headers is not None and not isinstance(headers, dict):
                        raise HTTPException(status_code=400, detail=f"Transform headers for domain '{domain}' must be a JSON object")
                    if isinstance(headers, dict):
                        for k, v in headers.items():
                            if not isinstance(k, str) or not isinstance(v, str):
                                raise HTTPException(status_code=400, detail=f"Header names and values must be strings (domain '{domain}')")


_state_write_lock = threading.Lock()


def _write_state(daemon: FlintDaemon) -> None:
    """Atomically write daemon state to JSON file.

    Requests run in a thread pool, so concurrent creates/kills call this at the
    same time. The lock keeps them from sharing the temp file, and because the
    snapshot is taken inside it, the last write always holds the latest state.
    """
    with _state_write_lock:
        state = {
            "pid": os.getpid(),
            "started_at": daemon.started_at,
            "golden_snapshot_ready": daemon.golden_ready,
            "vms": {},
        }
        if daemon.manager:
            for d in daemon.manager.list_dicts():
                state["vms"][d["vm_id"]] = d
        tmp_path = DAEMON_STATE_PATH + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(state, f)
        os.replace(tmp_path, DAEMON_STATE_PATH)


async def _agent_request_async(vm_id: str, method: str, path: str, body: bytes | None = None, timeout: float = 65) -> tuple[int, bytes]:
    """Async wrapper: runs backend transport in a worker thread."""
    mgr = _require_manager()
    _require_entry(vm_id)
    return await asyncio.to_thread(mgr.agent_request, vm_id, method, path, body, timeout)


# ── Endpoints ──────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    print("GET /health")
    daemon = _get_daemon()
    result = {"status": "ok", "golden_snapshot_ready": daemon.golden_ready, "backend_kind": daemon.backend.kind,
              "network_policy": daemon.backend.supports_network_policy}
    if daemon.storage:
        result["storage_backend"] = daemon.storage.kind
        result["storage_healthy"] = daemon.storage.is_running()
    if daemon.cluster:
        result["cluster_host_id"] = daemon.cluster.host_id
        result["host_class"] = daemon.cluster.host_class
    return result


def _running_count(mgr: SandboxManager) -> int:
    with mgr._lock:
        return sum(1 for e in mgr._sandboxes.values() if e.state != SandboxState.PAUSED)


class _Unfenced(RuntimeError):
    """A leased VM started but couldn't be fenced, so it was killed."""


@app.post("/vms")
async def create_vm(request: Request, template_id: str = DEFAULT_TEMPLATE_ID, allow_internet_access: bool = True, use_pool: bool = True, use_pyroute2: bool = True, tenant: str = ""):
    """Create a VM. A cluster control plane also passes, in the body:
    ``vm_id`` (it assigns IDs), ``tenant``, ``availability``, ``snapshot``
    (restore a paused/checkpointed/forked VM from the snapshot store instead
    of booting the template) and ``template_snapshot`` (where to fetch a
    template this host doesn't have yet)."""
    print(f"POST /vms — creating VM (template={template_id}, internet={allow_internet_access})...")
    daemon = _get_daemon()
    mgr = _require_manager()
    data: dict = {}
    body = await request.body()
    if body:
        try:
            data = json.loads(body) or {}
        except (json.JSONDecodeError, ValueError):
            raise HTTPException(status_code=400, detail="Invalid JSON in request body")
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="Request body must be a JSON object")
    network_policy = data.get("network_policy")
    if network_policy is not None:
        _validate_network_policy(network_policy)
    vm_id = data.get("vm_id") or None
    availability = data.get("availability") or ""
    tenant = data.get("tenant") or tenant
    if not daemon.cluster:  # the control plane resolved it already
        principal = principal_of(request)
        tenant = principal.resolve_tenant(tenant)
        tmpl = _template_for(principal, template_id)
        if tmpl is None or (tmpl.get("tenant") and tmpl["tenant"] != tenant):
            raise HTTPException(status_code=404, detail=f"Template '{template_id}' not found")
    snapshot = data.get("snapshot")
    template_snapshot = data.get("template_snapshot")
    if (vm_id or snapshot or template_snapshot) and not daemon.cluster:
        raise HTTPException(status_code=400, detail="vm_id and snapshots are only accepted from a cluster control plane")
    if template_id == DEFAULT_TEMPLATE_ID and not daemon.golden_ready and not snapshot:
        raise HTTPException(status_code=503, detail="Golden snapshot not ready")
    if daemon.cluster:
        from flint.cluster.agent import capacity
        if _running_count(mgr) >= capacity()["max_vms"]:
            # 507 tells the scheduler this host is full; it tries another.
            raise HTTPException(status_code=507, detail="host is full")

    def _create() -> str:
        vm_id = _start()
        if daemon.cluster:
            try:
                daemon.cluster.fence_started(vm_id)
            except Exception as e:
                daemon.kill_vm(vm_id)
                raise _Unfenced(f"VM {vm_id[:8]} not fenced, killed it: {e}") from e
        return vm_id

    def _start() -> str:
        if snapshot:
            return daemon.cluster.restore_vm(
                vm_id, snapshot, template_id=template_id, allow_internet_access=allow_internet_access,
                network_policy=network_policy, tenant=tenant, availability=availability,
            )
        if template_snapshot:
            daemon.cluster.ensure_template(template_id, template_snapshot)
        return mgr.create(
            template_id=template_id,
            allow_internet_access=allow_internet_access,
            use_pool=use_pool,
            use_pyroute2=use_pyroute2,
            network_policy=network_policy,
            vm_id=vm_id,
            tenant=tenant,
            availability=availability,
        )

    # Run in executor to avoid blocking the event loop (create can take 10s+ on failure)
    # The control plane enforces a cluster's quotas.
    reservation = daemon.quotas.reserve(tenant, lambda: _usage(tenant), _one_vm()) if not daemon.cluster else contextlib.nullcontext()
    try:
        with reservation:
            vm_id = await asyncio.to_thread(_create)  # to_thread keeps the caller (eventlog.actor)
    except QuotaExceeded:
        raise
    except _Unfenced as e:
        log.exception("POST /vms — %s", e)
        print(f"POST /vms — {e}")
        # 503: the scheduler places the VM on another host.
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        log.exception("POST /vms — VM creation failed")
        print(f"POST /vms — VM creation failed: {e}")
        raise HTTPException(status_code=500, detail=f"VM creation failed: {e}")

    # Set up storage for the new sandbox (no-op for local backend).
    if daemon.storage:
        entry = mgr.get_entry(vm_id)
        if entry:
            veth_ip = (entry.backend_metadata or {}).get("veth_ip", "")
            try:
                daemon.storage.setup_sandbox(
                    vm_id=vm_id,
                    template_id=template_id,
                    veth_ip=veth_ip,
                    ns_name=entry.ns_name,
                    agent_url=entry.agent_url,
                )
            except Exception as e:
                log.warning("Storage setup failed for %s: %s", vm_id[:8], e)

    if data.get("timeout_seconds"):
        mgr.set_timeout(vm_id, float(data["timeout_seconds"]), data.get("timeout_policy") or "kill")
    result = mgr.get_dict(vm_id) or {"vm_id": vm_id}
    if daemon.storage:
        result["storage_backend"] = daemon.storage.kind
        result["workspace_dir"] = WORKSPACE_DIR
    _write_state(daemon)
    _events.publish("vm.created", vm=result)
    how = f"snapshot '{snapshot['id']}'" if snapshot else f"template '{template_id}'"
    _logs.append(vm_id, f"VM created from {how} in {result.get('ready_time_ms', 0):.0f} ms")
    for step, ms in (result.get("timings") or {}).items():
        _logs.append(vm_id, f"  timing.{step} = {ms:.1f} ms")
    print(f"POST /vms — created {vm_id[:8]}")
    return {"vm": result}


@app.get("/vms")
def list_vms(request: Request, tenant: str = ""):
    daemon = _get_daemon()
    if not daemon.manager:
        return {"vms": []}
    principal = principal_of(request)
    entries = [d for d in daemon.manager.list_dicts()
               if principal.can_tenant(d.get("tenant", "")) and (not tenant or d.get("tenant") == tenant)]
    print(f"GET /vms — {len(entries)} VMs")
    return {"vms": entries}


@app.get("/vms/{vm_id}")
def get_vm(vm_id: str):
    mgr = _require_manager()
    result = mgr.get_dict(vm_id)
    if not result:
        print(f"GET /vms/{vm_id[:8]} — not found")
        raise HTTPException(status_code=404, detail="VM not found")
    return {"vm": result}


@app.delete("/vms/{vm_id}")
def delete_vm(vm_id: str):
    print(f"DELETE /vms/{vm_id[:8]}")
    daemon = _get_daemon()
    mgr = _require_manager()
    if mgr.get_dict(vm_id) is None:
        print(f"DELETE /vms/{vm_id[:8]} — not found")
        raise HTTPException(status_code=404, detail="VM not found")
    daemon.kill_vm(vm_id)
    print(f"DELETE /vms/{vm_id[:8]} — killed")
    return {"ok": True}


async def _optional_json(request: Request) -> dict:
    body = await request.body()
    if not body:
        return {}
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid JSON in request body")
    return data if isinstance(data, dict) else {}


@app.post("/vms/{vm_id}/pause")
async def pause_vm(vm_id: str, request: Request):
    """Pause. In a cluster, ``{"snapshot_id": ...}`` also exports the paused
    state to the snapshot store so the VM can be resumed on any host."""
    print(f"POST /vms/{vm_id[:8]}/pause")
    daemon = _get_daemon()
    mgr = _require_manager()
    data = await _optional_json(request)
    snapshot_id = data.get("snapshot_id")
    if snapshot_id and not daemon.cluster:
        raise HTTPException(status_code=400, detail="snapshot export needs cluster mode")

    def _pause() -> dict | None:
        if not snapshot_id:
            mgr.pause(vm_id)
            return None
        with daemon.cluster.api_pause(vm_id):
            mgr.pause(vm_id)
            try:
                return daemon.cluster.export_pause(vm_id, snapshot_id, upload=data.get("upload") or "sync")
            except Exception:
                # The control plane will treat this pause as failed: leave the VM
                # running, as it believes, rather than paused with no snapshot.
                log.exception("[%s] pause export failed; resuming", vm_id[:8])
                mgr.resume(vm_id)
                raise

    try:
        snap = await asyncio.to_thread(_pause)
    except Exception as e:
        log.exception("POST /vms/%s/pause — failed", vm_id[:8])
        print(f"POST /vms/{vm_id[:8]}/pause — failed: {e}")
        raise HTTPException(status_code=500, detail=f"Pause failed: {e}")
    _write_state(daemon)
    _events.publish("vm.paused", vm_id=vm_id)
    _logs.append(vm_id, "VM paused")
    print(f"POST /vms/{vm_id[:8]}/pause — paused")
    result: dict = {"ok": True}
    if snap:
        result["snapshot"] = snap
    return result


@app.post("/vms/{vm_id}/resume")
def resume_vm(vm_id: str):
    print(f"POST /vms/{vm_id[:8]}/resume")
    daemon = _get_daemon()
    mgr = _require_manager()
    try:
        mgr.resume(vm_id)
    except Exception as e:
        log.exception("POST /vms/%s/resume — failed", vm_id[:8])
        print(f"POST /vms/{vm_id[:8]}/resume — failed: {e}")
        raise HTTPException(status_code=500, detail=f"Resume failed: {e}")
    if daemon.cluster:
        try:
            daemon.cluster.fence_started(vm_id)
        except Exception as e:
            # Running unfenced could leave two copies; paused, it's safe here.
            log.exception("POST /vms/%s/resume — not fenced", vm_id[:8])
            mgr.pause(vm_id)
            raise HTTPException(status_code=503, detail=f"VM resumed but not fenced, paused it again: {e}")
        daemon.cluster.forget_pause(vm_id)
    result = mgr.get_dict(vm_id) or {"vm_id": vm_id}
    _write_state(daemon)
    _events.publish("vm.resumed", vm=result)
    _logs.append(vm_id, "VM resumed")
    print(f"POST /vms/{vm_id[:8]}/resume — resumed")
    return {"vm": result}


@app.post("/vms/{vm_id}/checkpoint")
async def checkpoint_vm(vm_id: str, request: Request):
    """Cluster only: snapshot a running VM without stopping it (fork, durable VMs)."""
    daemon = _get_daemon()
    if not daemon.cluster:
        raise HTTPException(status_code=400, detail="checkpoints need cluster mode")
    _require_entry(vm_id)
    data = await _optional_json(request)
    if not data.get("snapshot_id"):
        raise HTTPException(status_code=400, detail="snapshot_id is required")
    try:
        result = await asyncio.to_thread(
            lambda: daemon.cluster.checkpoint(
                vm_id, data["snapshot_id"], data.get("kind") or "checkpoint",
                parent=data.get("parent") or "", upload=data.get("upload") or "sync",
            ),
        )
    except Exception as e:
        log.exception("POST /vms/%s/checkpoint — failed", vm_id[:8])
        raise HTTPException(status_code=500, detail=f"Checkpoint failed: {e}")
    _events.publish("vm.checkpoint", vm_id=vm_id, snapshot_id=data["snapshot_id"], timings=result.get("timings"))
    return result


@app.patch("/vms/{vm_id}")
def patch_vm(vm_id: str, body: dict):
    mgr = _require_manager()
    if mgr.get_dict(vm_id) is None:
        raise HTTPException(status_code=404, detail="VM not found")
    timeout = body.get("timeout_seconds")
    policy = body.get("timeout_policy", "kill")
    if timeout is not None:
        mgr.set_timeout(vm_id, float(timeout), policy)
    return {"ok": True}


@app.get("/vms/{vm_id}/metrics")
def get_vm_metrics(vm_id: str, window: int = 60):
    daemon = _get_daemon()
    if daemon.metrics is None:
        raise HTTPException(status_code=503, detail="Metrics sampler not initialized")
    _require_entry(vm_id)
    samples = daemon.metrics.get(vm_id, window=window)
    return {"samples": samples}


# ── Network policy endpoints ──────────────────────────────────────────────

@app.put("/vms/{vm_id}/network-policy")
async def update_network_policy(vm_id: str, request: Request):
    """Update the network policy (credential injection rules) for a sandbox."""
    print(f"PUT /vms/{vm_id[:8]}/network-policy")
    mgr = _require_manager()
    _require_entry(vm_id)
    try:
        body = json.loads(await request.body())
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid JSON in request body")
    _validate_network_policy(body)
    if not _get_daemon().backend.supports_network_policy:
        raise HTTPException(status_code=501, detail=f"Network policy isn't supported on the {_get_daemon().backend.name} backend")
    mgr.update_network_policy(vm_id, body)
    print(f"PUT /vms/{vm_id[:8]}/network-policy — updated")
    return {"ok": True}


@app.get("/vms/{vm_id}/network-policy")
def get_network_policy(vm_id: str):
    """The sandbox's network policy. Credential values are write-only: redacted here."""
    mgr = _require_manager()
    _require_entry(vm_id)
    return {"network_policy": mgr.get_network_policy(vm_id)}


# ── Guest agent proxy endpoints ───────────────────────────────────────────

def _command_text(body: bytes) -> str:
    """The command an exec or process request runs, for its event (``sh -c`` scripts as is)."""
    import shlex
    try:
        cmd = json.loads(body).get("cmd") or []
    except (ValueError, AttributeError):
        return ""
    if not isinstance(cmd, list):
        return str(cmd)[:500]
    if len(cmd) >= 3 and cmd[1] == "-c" and cmd[0].endswith("sh"):
        return str(cmd[2])[:500]
    return shlex.join(str(c) for c in cmd)[:500]


@app.post("/vms/{vm_id}/exec")
async def exec_command(vm_id: str, request: Request):
    """Proxy POST /exec to the guest agent, and record it as a ``vm.exec`` event."""
    print(f"POST /vms/{vm_id[:8]}/exec")
    body = await request.body()
    t0 = time.monotonic()
    status, resp_body = await _agent_request_async(vm_id, "POST", "/exec", body)
    event = {"vm_id": vm_id, "cmd": _command_text(body), "duration_ms": round((time.monotonic() - t0) * 1000, 1)}
    try:
        result = json.loads(resp_body) if status == 200 else {}
        event.update(exit_code=result.get("exit_code"), stdout_tail=str(result.get("stdout", ""))[-256:],
                     stderr_tail=str(result.get("stderr", ""))[-256:])
    except ValueError:
        pass
    if status != 200:
        event["error"] = resp_body[:256].decode(errors="replace")
    _events.publish("vm.exec", **event)
    return Response(content=resp_body, status_code=status, media_type="application/json")


@app.post("/vms/{vm_id}/processes")
async def create_process(vm_id: str, request: Request):
    """Proxy POST /processes to the guest agent."""
    print(f"POST /vms/{vm_id[:8]}/processes")
    body = await request.body()
    status, resp_body = await _agent_request_async(vm_id, "POST", "/processes", body)
    if status == 200:
        try:
            pid = json.loads(resp_body).get("pid")
        except ValueError:
            pid = None
        _events.publish("vm.process_started", vm_id=vm_id, cmd=_command_text(body), pid=pid)
    return Response(content=resp_body, status_code=status, media_type="application/json")


@app.get("/vms/{vm_id}/processes")
async def list_processes(vm_id: str):
    """Proxy GET /processes to the guest agent."""
    status, resp_body = await _agent_request_async(vm_id, "GET", "/processes")
    return Response(content=resp_body, status_code=status, media_type="application/json")


@app.post("/vms/{vm_id}/processes/{pid}/input")
async def process_input(vm_id: str, pid: int, request: Request):
    """Proxy stdin input to a guest process."""
    body = await request.body()
    status, resp_body = await _agent_request_async(vm_id, "POST", f"/processes/{pid}/input", body)
    return Response(content=resp_body, status_code=status, media_type="application/json")


@app.post("/vms/{vm_id}/processes/{pid}/signal")
async def process_signal(vm_id: str, pid: int, request: Request):
    """Proxy signal to a guest process."""
    body = await request.body()
    status, resp_body = await _agent_request_async(vm_id, "POST", f"/processes/{pid}/signal", body)
    return Response(content=resp_body, status_code=status, media_type="application/json")


@app.post("/vms/{vm_id}/processes/{pid}/resize")
async def process_resize(vm_id: str, pid: int, request: Request):
    """Proxy resize to a guest process."""
    body = await request.body()
    status, resp_body = await _agent_request_async(vm_id, "POST", f"/processes/{pid}/resize", body)
    return Response(content=resp_body, status_code=status, media_type="application/json")


# ── Filesystem proxy endpoints ───────────────────────────────────────────

@app.get("/vms/{vm_id}/files")
async def read_file(vm_id: str, path: str):
    """Proxy GET /files to the guest agent."""
    status, resp_body = await _agent_request_async(vm_id, "GET", f"/files?path={urllib.parse.quote(path, safe='/')}")
    media = "application/octet-stream" if status == 200 else "application/json"
    return Response(content=resp_body, status_code=status, media_type=media)


@app.post("/vms/{vm_id}/files")
async def write_file(vm_id: str, path: str, request: Request, mode: str = "0644"):
    """Proxy POST /files to the guest agent."""
    body = await request.body()
    status, resp_body = await _agent_request_async(vm_id, "POST", f"/files?path={urllib.parse.quote(path, safe='/')}&mode={mode}", body)
    return Response(content=resp_body, status_code=status, media_type="application/json")


@app.get("/vms/{vm_id}/files/stat")
async def stat_file(vm_id: str, path: str):
    status, resp_body = await _agent_request_async(vm_id, "GET", f"/files/stat?path={urllib.parse.quote(path, safe='/')}")
    return Response(content=resp_body, status_code=status, media_type="application/json")


@app.get("/vms/{vm_id}/files/list")
async def list_files(vm_id: str, path: str):
    status, resp_body = await _agent_request_async(vm_id, "GET", f"/files/list?path={urllib.parse.quote(path, safe='/')}")
    return Response(content=resp_body, status_code=status, media_type="application/json")


@app.post("/vms/{vm_id}/files/mkdir")
async def mkdir(vm_id: str, path: str, parents: bool = True):
    status, resp_body = await _agent_request_async(vm_id, "POST", f"/files/mkdir?path={urllib.parse.quote(path, safe='/')}&parents={'true' if parents else 'false'}")
    return Response(content=resp_body, status_code=status, media_type="application/json")


@app.delete("/vms/{vm_id}/files")
async def delete_file(vm_id: str, path: str, recursive: bool = False):
    status, resp_body = await _agent_request_async(vm_id, "DELETE", f"/files?path={urllib.parse.quote(path, safe='/')}&recursive={'true' if recursive else 'false'}")
    return Response(content=resp_body, status_code=status, media_type="application/json")


# ── Terminal WebSocket ────────────────────────────────────────────────────

@app.websocket("/vms/{vm_id}/terminal")
async def terminal_ws(websocket: WebSocket, vm_id: str):
    """Interactive terminal: delegated to the active backend."""
    print(f"WS /vms/{vm_id[:8]}/terminal — connecting")
    daemon = _get_daemon()
    if not daemon.manager:
        await websocket.close(code=1011, reason="Manager not initialized")
        return
    try:
        await daemon.manager.bridge_terminal(vm_id, websocket)
    except RuntimeError:
        print(f"WS /vms/{vm_id[:8]}/terminal — VM not found")
        await websocket.close(code=1011, reason="VM not found")
    except WebSocketDisconnect:
        print(f"WS /vms/{vm_id[:8]}/terminal — client disconnected")
    except Exception:
        log.exception("WS /vms/%s/terminal — error", vm_id[:8])


# ── Events broadcast WebSocket ────────────────────────────────────────────

async def _forward(websocket: WebSocket, q: asyncio.Queue, send) -> None:
    """Send queue items to the client until it disconnects.

    Also waits on the socket, so the handler ends when the client goes away
    or the server closes it on shutdown, instead of blocking on the queue.
    """
    receive = asyncio.ensure_future(websocket.receive())
    get = asyncio.ensure_future(q.get())
    try:
        while True:
            done, _ = await asyncio.wait({receive, get}, return_when=asyncio.FIRST_COMPLETED)
            if get in done:
                await send(get.result())
                get = asyncio.ensure_future(q.get())
            if receive in done:
                if receive.result()["type"] == "websocket.disconnect":
                    return
                receive = asyncio.ensure_future(websocket.receive())  # clients send nothing we use
    finally:
        receive.cancel()
        get.cancel()


@app.websocket("/events")
async def events_ws(websocket: WebSocket):
    """Stream daemon-wide lifecycle events to a subscriber."""
    bus = _events.get_bus()
    if bus is None:
        await websocket.close(code=1011, reason="Event bus not initialized")
        return
    principal = principal_of(websocket)
    await websocket.accept()
    q = bus.subscribe()

    async def send(event: dict) -> None:
        tenant = event.get("tenant")
        # An event of no known tenant only goes to callers who see every tenant.
        if principal.can_tenant(tenant) if tenant else (principal.is_admin or not principal.tenants):
            await websocket.send_json(event)

    try:
        await _forward(websocket, q, send)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("WS /events — error")
    finally:
        bus.unsubscribe(q)


# ── Per-VM log stream ──────────────────────────────────────────────────────

@app.websocket("/vms/{vm_id}/logs")
async def logs_ws(websocket: WebSocket, vm_id: str):
    bus = _logs.get_bus()
    if bus is None:
        await websocket.close(code=1011, reason="Log bus not initialized")
        return
    await websocket.accept()
    # Send history first so late subscribers see recent context.
    for line in bus.history(vm_id):
        await websocket.send_text(line)
    q = bus.subscribe(vm_id)
    try:
        await _forward(websocket, q, websocket.send_text)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("WS /vms/%s/logs — error", vm_id[:8])
    finally:
        bus.unsubscribe(vm_id, q)


# ── Template endpoints ──────────────────────────────────────────────────────

_build_threads: dict[str, threading.Thread] = {}


@app.post("/templates/build")
def build_template_endpoint(body: dict, request: Request):
    """Build a template. With ``tenant`` (or a token pinned to one tenant) it
    belongs to that tenant; without, every tenant can use it."""
    name = body.get("name")
    dockerfile = body.get("dockerfile")
    rootfs_size_mb = body.get("rootfs_size_mb", 500)
    if not name or not dockerfile:
        raise HTTPException(status_code=400, detail="name and dockerfile are required")

    from flint.core._template_build import _slugify
    from flint.core._template_registry import claim_template
    template_id = _slugify(name)
    if template_id == DEFAULT_TEMPLATE_ID:
        raise HTTPException(status_code=400, detail="The default template can't be rebuilt this way")
    daemon = _get_daemon()
    tenant = ""
    if not daemon.cluster:
        principal = principal_of(request)
        pinned = principal.tenants and not principal.is_admin
        tenant = principal.resolve_tenant(body.get("tenant")) if (body.get("tenant") or pinned) else ""
        existing = get_template(template_id)
        if existing is not None and existing.get("tenant", "") != tenant:
            visible = _template_for(principal, template_id) is not None
            raise HTTPException(status_code=409 if visible else 403,
                                detail=f"Template '{template_id}' already exists" + ("" if visible else " for another tenant"))
        claim_template(template_id, name, tenant)
    print(f"POST /templates/build — building {template_id}...")

    if daemon.cluster:
        daemon.cluster.forget_template(template_id)  # a rebuild gets published again

    def _run_build():
        try:
            daemon.backend.build_template(name, dockerfile, rootfs_size_mb=rootfs_size_mb)
            if not daemon.cluster:
                # Builds re-create a template's entry if it went meanwhile: keep the owner.
                claim_template(template_id, name, tenant)
            print(f"Template {template_id} built successfully")
        except Exception as e:
            print(f"Template {template_id} build failed: {e}")
            log.exception("Template build failed: %s", template_id)

    t = threading.Thread(target=_run_build, daemon=True, name=f"build-{template_id}")
    t.start()
    _build_threads[template_id] = t

    return {"template_id": template_id, "status": "building"}


@app.get("/templates")
def list_templates_endpoint(request: Request):
    principal = principal_of(request)
    templates = [t for t in list_templates() if not t.get("tenant") or principal.can_tenant(t["tenant"])]
    print(f"GET /templates — {len(templates)} templates")
    return {"templates": templates}


@app.get("/templates/{template_id}")
def get_template_endpoint(template_id: str, request: Request):
    tmpl = _template_for(principal_of(request), template_id)
    if tmpl is None:
        raise HTTPException(status_code=404, detail="Template not found")
    return {"template": tmpl}


@app.delete("/templates/{template_id}")
def delete_template_endpoint(template_id: str, request: Request):
    print(f"DELETE /templates/{template_id}")
    if template_id == DEFAULT_TEMPLATE_ID:
        raise HTTPException(status_code=400, detail="Cannot delete default template")
    principal = principal_of(request)
    tmpl = _template_for(principal, template_id)
    if tmpl is None:
        raise HTTPException(status_code=404, detail="Template not found")
    if not tmpl.get("tenant") and principal.tenants and not principal.is_admin:
        raise HTTPException(status_code=403, detail="Shared templates can only be deleted by an admin")
    daemon = _get_daemon()
    if daemon.cluster:
        daemon.cluster.forget_template(template_id)
    daemon.backend.delete_template_artifact(template_id, tmpl)
    artifacts = tmpl.get("artifacts") or {}
    for artifact in artifacts.values():
        tdir = artifact.get("template_dir", "")
        if tdir and os.path.isdir(tdir):
            shutil.rmtree(tdir, ignore_errors=True)
    _delete_template(template_id)
    print(f"DELETE /templates/{template_id} — deleted")
    return {"ok": True}


# ── Volume endpoints ──────────────────────────────────────────────────────

def _require_volumes() -> Volumes:
    daemon = _get_daemon()
    if daemon.volumes is None:
        raise HTTPException(status_code=503, detail="Volumes not initialized")
    return daemon.volumes


def _volume_call(fn, *args):
    try:
        return fn(*args)
    except VolumeError as e:
        raise HTTPException(status_code=e.status, detail=str(e))


def _scope(principal: Principal) -> tuple[str, ...] | None:
    """The tenants a caller's lookups are limited to (None: every tenant)."""
    return None if principal.is_admin or not principal.tenants else principal.tenants


@app.get("/volumes")
def list_volumes(request: Request, tenant: str = ""):
    vols = _require_volumes().list(_scope(principal_of(request)))
    return {"volumes": [v.to_dict() for v in vols if not tenant or v.tenant == tenant]}


@app.post("/volumes")
def create_volume(body: dict, request: Request):
    tenant = principal_of(request).resolve_tenant(body.get("tenant"))
    from flint.core import _nfs_server
    size = body.get("size_gib") if isinstance(body.get("size_gib"), int) else 0
    if _nfs_server.images_available():
        size = size or DEFAULT_SIZE_GIB
    with _get_daemon().quotas.reserve(tenant, lambda: _usage(tenant), Usage(volume_gib=size)):
        vol = _volume_call(_require_volumes().create, body.get("name"), body.get("size_gib"), tenant)
    _events.publish("volume.created", volume=vol.to_dict())
    print(f"POST /volumes — created {vol.id} ({vol.name})")
    return {"volume": vol.to_dict()}


def _copy_volume(copy, volume: str, body: dict, request: Request):
    """Snapshot or fork: a copy counts its full size against the tenant's quota."""
    scope = _scope(principal_of(request))
    src = _volume_call(_require_volumes()._require, volume, scope)
    with _get_daemon().quotas.reserve(src.tenant, lambda: _usage(src.tenant), Usage(volume_gib=src.size_gib or 0)):
        return _volume_call(copy, volume, body.get("name"), scope)


@app.post("/volumes/{volume}/snapshot")
def snapshot_volume(volume: str, body: dict, request: Request):
    """A read-only copy of the volume as it is now."""
    vol = _copy_volume(_require_volumes().snapshot, volume, body, request)
    _events.publish("volume.created", volume=vol.to_dict())
    print(f"POST /volumes/{volume}/snapshot — {vol.id} ({vol.name})")
    return {"volume": vol.to_dict()}


@app.post("/volumes/{volume}/fork")
def fork_volume(volume: str, body: dict, request: Request):
    """A writable copy of the volume as it is now."""
    vol = _copy_volume(_require_volumes().fork, volume, body, request)
    _events.publish("volume.created", volume=vol.to_dict())
    print(f"POST /volumes/{volume}/fork — {vol.id} ({vol.name})")
    return {"volume": vol.to_dict()}


@app.delete("/volumes/{volume}")
def delete_volume(volume: str, request: Request):
    vol = _volume_call(_require_volumes().delete, volume, _scope(principal_of(request)))
    _events.publish("volume.deleted", volume_id=vol.id, tenant=vol.tenant)
    print(f"DELETE /volumes/{vol.id} — deleted")
    return {"ok": True}


@app.get("/vms/{vm_id}/mounts")
def list_mounts(vm_id: str):
    return {"mounts": [m.to_dict() for m in _require_volumes().list_mounts(vm_id)]}


@app.post("/vms/{vm_id}/mounts")
def mount_volume(vm_id: str, body: dict):
    """Mount a volume (or a subpath of it) into a running sandbox."""
    m = _volume_call(
        _require_volumes().mount, vm_id, body.get("volume"), body.get("path"),
        body.get("subpath", ""), body.get("readonly", False), body.get("exclusive", False),
    )
    _events.publish("volume.attached", vm_id=vm_id, volume_id=m.volume_id, mount_path=m.path)
    mode = "exclusive" if m.exclusive else ("ro" if m.readonly else "rw")
    _logs.append(vm_id, f"Mounted volume {m.volume_id}:/{m.subpath} at {m.path} ({mode})")
    print(f"POST /vms/{vm_id[:8]}/mounts — {m.volume_id} at {m.path}")
    return {"mount": m.to_dict()}


@app.delete("/vms/{vm_id}/mounts")
def unmount_volume(vm_id: str, path: str):
    m = _volume_call(_require_volumes().unmount, vm_id, path)
    _events.publish("volume.detached", vm_id=vm_id, volume_id=m.volume_id, mount_path=m.path)
    _logs.append(vm_id, f"Unmounted {m.path}")
    print(f"DELETE /vms/{vm_id[:8]}/mounts — {m.path}")
    return {"ok": True}


# ── Stored events and the audit log ───────────────────────────────────────

def _require_eventlog():
    daemon = _get_daemon()
    if daemon.eventlog is None:
        raise HTTPException(status_code=503, detail="Event log not initialized")
    return daemon.eventlog


@app.get("/v1/events")
def list_events(request: Request, since: int = 0, vm_id: str = "", tenant: str = "", type: str = "",
                actor: str = "", limit: int = 500):
    """Stored events after ``since`` (a ``seq``), oldest first, of the caller's
    tenants. ``next`` is the ``since`` for the following page."""
    events = _require_eventlog().query(since=since, tenants=_scope(principal_of(request)), tenant=tenant,
                                       vm_id=vm_id, type=type, actor=actor, limit=limit)
    return {"events": events, "next": events[-1]["seq"] if events else since}


@app.get("/vms/{vm_id}/timeline")
def vm_timeline(vm_id: str, request: Request, since: int = 0, limit: int = 1000):
    """Everything that happened to a VM: lifecycle, execs, mounts, with who did it."""
    if _require_manager().get_dict(vm_id) is None:
        raise HTTPException(status_code=404, detail="VM not found")
    events = _require_eventlog().query(since=since, tenants=_scope(principal_of(request)), vm_id=vm_id, limit=limit)
    return {"events": events, "next": events[-1]["seq"] if events else since}


@app.get("/v1/audit")
def audit_log(since: int = 0, limit: int = 500):
    """The audit log (admin only): every API call that changed something, hash-chained."""
    entries = _require_eventlog().audit_entries(since, limit)
    return {"entries": entries, "next": entries[-1]["seq"] if entries else since}


@app.get("/v1/audit/verify")
def audit_verify():
    """Recompute the audit chain; ``ok`` is false, with where, if an entry was changed or removed."""
    return _require_eventlog().verify_audit()


# ── API tokens (admin only) ───────────────────────────────────────────────
# The same API as the cluster control plane's.

def _require_tokens() -> TokenStore:
    daemon = _get_daemon()
    if daemon.tokens is None:
        raise HTTPException(status_code=503, detail="Tokens not initialized")
    return daemon.tokens


@app.post("/v1/tokens")
def create_token(body: dict):
    """Create an ``admin`` or ``service`` token (``tenants`` pins a service
    token to those tenants). The token is in the answer, and only there."""
    tenants = body.get("tenants") or []
    if not isinstance(tenants, list):
        raise HTTPException(status_code=400, detail="'tenants' must be a list")
    record, token = _require_tokens().create(body.get("kind") or "service", body.get("name") or "", tenants)
    print(f"POST /v1/tokens — created {record['kind']} token {record['id']}")
    return {**record, "token": token}


@app.get("/v1/tokens")
def list_tokens():
    return {"tokens": _require_tokens().list()}


@app.delete("/v1/tokens/{token_id}")
def revoke_token(token_id: str):
    if not _require_tokens().revoke(token_id):
        raise HTTPException(status_code=404, detail="Token not found")
    print(f"DELETE /v1/tokens/{token_id} — revoked")
    return {"ok": True}


# ── Tenant quotas ─────────────────────────────────────────────────────────
# The same API as the cluster control plane's, with more limits.

@app.put("/v1/tenants/{tenant}/quota")
def put_quota(tenant: str, body: dict, request: Request):
    """Set some of a tenant's limits (admin only): max_vms, max_vcpus,
    max_memory_mib, max_volume_gib. 0 = no limit; the others stay."""
    if not principal_of(request).is_admin:
        raise HTTPException(status_code=403, detail="Admin token required")
    try:
        _get_daemon().quotas.set(tenant, body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _get_daemon().quotas.view(tenant, _usage(tenant))


@app.get("/v1/tenants/{tenant}/quota")
def get_quota(tenant: str, request: Request):
    """A tenant's limits and what it uses now."""
    if not principal_of(request).can_tenant(tenant):
        raise HTTPException(status_code=403, detail="Not allowed for this tenant")
    return _get_daemon().quotas.view(tenant, _usage(tenant))


# ── Config / limits endpoints ─────────────────────────────────────────────

def _current_config_view() -> dict:
    from flint.core import config as c
    return {
        "pool_target_size": c.POOL_TARGET_SIZE,
        "default_sandbox_timeout": c.DEFAULT_SANDBOX_TIMEOUT,
        "health_check_interval": c.HEALTH_CHECK_INTERVAL,
        "error_cleanup_delay": c.ERROR_CLEANUP_DELAY,
    }


def _load_config_overrides() -> dict:
    from flint.core.config import CONFIG_OVERRIDES_PATH
    try:
        with open(CONFIG_OVERRIDES_PATH) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


@app.get("/config")
def get_config():
    return {"config": _current_config_view(), "overrides": _load_config_overrides()}


@app.patch("/config")
def patch_config(body: dict):
    from flint.core.config import CONFIG_FIELDS, CONFIG_OVERRIDES_PATH
    overrides = _load_config_overrides()
    for k, v in body.items():
        meta = CONFIG_FIELDS.get(k)
        if not meta:
            raise HTTPException(status_code=400, detail=f"Unknown field: {k}")
        _const_name, typ = meta
        try:
            overrides[k] = typ(v)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"Invalid type for {k}")
    os.makedirs(os.path.dirname(CONFIG_OVERRIDES_PATH), exist_ok=True)
    tmp_path = CONFIG_OVERRIDES_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(overrides, f)
    os.rename(tmp_path, CONFIG_OVERRIDES_PATH)
    return {
        "ok": True,
        "overrides": overrides,
        "requires_restart": list(body.keys()),
    }


# ── Web UI ─────────────────────────────────────────────────────────────────
# The admin UI (web/) is built into src/flint/daemon/ui and served from the same
# origin as the API, so it needs no CORS. FLINT_UI_DIR overrides the location.

_UI_DIR = os.path.realpath(os.environ.get("FLINT_UI_DIR") or os.path.join(os.path.dirname(__file__), "ui"))
_UI_MISSING = """<!doctype html><meta charset="utf-8"><title>Flint</title>
<body style="font:14px system-ui;background:#151922;color:#eef1f7;display:grid;place-items:center;height:100vh;margin:0">
<div><h1 style="font-size:18px">The Flint web UI isn't built</h1>
<p>Build it with <code>scripts/build-ui.sh</code> (needs Node 20+), then reload.</p></div>"""


@app.get("/", include_in_schema=False)
def root_redirect():
    return RedirectResponse("/ui/")


@app.get("/ui", include_in_schema=False)
@app.get("/ui/{path:path}", include_in_schema=False)
def web_ui(path: str = ""):
    index = os.path.join(_UI_DIR, "index.html")
    if not os.path.isfile(index):
        return HTMLResponse(_UI_MISSING, status_code=503)
    target = os.path.realpath(os.path.join(_UI_DIR, path))
    if path and target.startswith(_UI_DIR + os.sep) and os.path.isfile(target):
        # Vite fingerprints everything under assets/, so it can be cached forever.
        cache = "public, max-age=31536000, immutable" if "/assets/" in target else "no-cache"
        return FileResponse(target, headers={"Cache-Control": cache})
    # Client-side routes (/ui/vms/abc) all load the app shell.
    return FileResponse(index, headers={"Cache-Control": "no-cache"})


# ── Listening sockets ────────────────────────────────────────────────────

def _listen_unix(path: str):
    """The daemon's Unix socket: only its user, and the ``flint`` group if there
    is one, can connect, and whoever connects is an admin."""
    import grp
    import socket
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.connect(path)
        raise SystemExit(f"Another Flint daemon is listening on {path}")
    except (FileNotFoundError, ConnectionRefusedError):
        pass
    finally:
        probe.close()
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old_umask = os.umask(0o177)  # never connectable by others, even briefly
    try:
        sock.bind(path)
    finally:
        os.umask(old_umask)
    try:
        os.chown(path, -1, grp.getgrnam("flint").gr_gid)
        os.chmod(path, 0o660)
    except (KeyError, PermissionError):
        os.chmod(path, 0o600)
    sock.listen(2048)
    sock.set_inheritable(True)
    return sock


def _listen_tcp(host: str, port: int):
    import socket
    sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(2048)
    sock.set_inheritable(True)
    return sock


# ── FlintDaemon ──────────────────────────────────────────────────────────

class FlintDaemon:
    def __init__(self) -> None:
        self.backend = get_host_backend()
        self.manager: SandboxManager | None = None
        self.golden_ready: bool = False
        self.started_at: float = 0.0
        self._state_store = None
        self._health_monitor = None
        self._lifecycle_manager = None
        self.storage = None
        self.volumes: Volumes | None = None
        self.metrics: MetricsSampler | None = None
        self.cluster = None  # ClusterAgent when running as a cluster host agent
        self.tokens: TokenStore | None = None
        self.eventlog = None  # eventlog.EventLog
        self.quotas: Quotas | None = None

    def kill_vm(self, vm_id: str) -> None:
        """Delete a sandbox (running or paused) and everything it holds."""
        mgr = self.manager
        if self.storage:
            entry = mgr.get_entry(vm_id)
            veth_ip = (entry.backend_metadata or {}).get("veth_ip", "") if entry else ""
            self.storage.teardown_sandbox(vm_id, veth_ip)
        mgr.kill(vm_id)
        if self.cluster:
            self.cluster.forget_pause(vm_id)
        _write_state(self)
        _events.publish("vm.deleted", vm_id=vm_id)
        _logs.append(vm_id, "VM killed")

    def run(self) -> None:
        self.started_at = time.time()
        app.state.daemon = self

        self._init_dirs()
        self._write_pid()
        self._prepare_backend()
        self._init_nfs_server()
        self._init_storage()
        self._start_pool()
        self._init_state_store()
        self._init_sso()
        self._init_manager()
        self._recover_sandboxes()
        self.volumes.replay()
        self._start_health_monitor()
        self._start_lifecycle()
        self._start_cluster_agent()
        self._install_signal_handlers()
        self._serve()

    def _start_cluster_agent(self) -> None:
        from flint.core import config as _cfg
        if not _cfg.CLUSTER_MODE:
            return
        from flint.cluster.agent import ClusterAgent
        self.cluster = ClusterAgent(self)
        self.cluster.start()
        print(f"Cluster host agent: host {self.cluster.host_id}, control plane {self.cluster.server}")

    def _init_dirs(self) -> None:
        os.makedirs(DAEMON_DIR, exist_ok=True)

    def _write_pid(self) -> None:
        with open(DAEMON_PID_PATH, "w") as f:
            f.write(str(os.getpid()))

    def _prepare_backend(self) -> None:
        os.makedirs(TEMPLATES_DIR, exist_ok=True)
        self.backend.ensure_runtime_ready()
        self.backend.ensure_default_template()
        default_template = get_template(DEFAULT_TEMPLATE_ID) or {}
        artifact = (default_template.get("artifacts") or {}).get(self.backend.kind) or {}
        self.golden_ready = artifact.get("status") == "ready"
        print(f"Backend ready: {self.backend.kind}")

    def _init_nfs_server(self) -> None:
        if self.backend.kind.startswith("macos"):
            return  # No bridge network to serve volumes on.
        try:
            _nfs_server.ensure_running()
            print("NFS server ready.")
        except _nfs_server.NfsServerError as e:
            print(f"NFS server unavailable, volumes can't be mounted: {e}")

    def _init_storage(self) -> None:
        from flint.core.storage import get_storage_backend
        self.storage = get_storage_backend()
        self.storage.start()
        print(f"Storage backend: {self.storage.kind}")

    def _start_pool(self) -> None:
        self.backend.start_pool()
        print("Backend pool started.")

    def _init_sso(self) -> None:
        """Sign-in with identity providers, if ``sso.json`` configures any."""
        from flint.core import config as _cfg
        if _cfg.CLUSTER_MODE:
            return  # the control plane signs people in
        try:
            sso = _sso.load()
        except ValueError as e:
            raise SystemExit(f"SSO config: {e}")
        if sso is None:
            return
        from starlette.middleware.sessions import SessionMiddleware
        from flint.core._secrets import derived_key
        app.add_middleware(SessionMiddleware, secret_key=derived_key("sso-login"), session_cookie=_sso.LOGIN_COOKIE,
                           max_age=600, same_site="lax", https_only=sso.secure_cookies)
        app.state.sso = sso
        for p in sso.config.providers.values():
            print(f"SSO: {p.label} ({p.id}); its redirect URI is {sso.redirect_uri(p.id)}")

    def _init_state_store(self) -> None:
        from flint.core._state_store import StateStore
        self._state_store = StateStore(DAEMON_DB_PATH)
        self.tokens = TokenStore(DAEMON_DB_PATH)
        from flint.daemon.eventlog import EventLog
        self.eventlog = EventLog(DAEMON_DB_PATH)
        _events.sink = self.eventlog.record
        self.quotas = Quotas(DAEMON_DB_PATH)
        from flint.core import config as _cfg
        if not _cfg.CLUSTER_MODE:  # a host agent's only caller is the control plane, over mTLS
            self.tokens.ensure_admin_token_file()
        print("State store initialized.")

    def _init_manager(self) -> None:
        self.manager = SandboxManager(backend=self.backend, state_store=self._state_store)
        self.volumes = Volumes(self._state_store._conn, self.manager, self._state_store)
        self.manager.kill_hooks.append(self._release_mounts)
        self.metrics = MetricsSampler(lambda: list(self.manager._sandboxes.values()) if self.manager else [], self.backend.usage)
        _events.vm_tenant = lambda vm_id: (self.manager.get_dict(vm_id) or {}).get("tenant", "")
        _write_state(self)

    def _release_mounts(self, vm_id: str) -> None:
        for m in self.volumes.release_vm(vm_id):
            _events.publish("volume.detached", vm_id=vm_id, volume_id=m.volume_id, mount_path=m.path)

    def _recover_sandboxes(self) -> None:
        if not self._state_store or not self.manager:
            return
        from flint.core._recovery import RecoveryEngine
        engine = RecoveryEngine(self._state_store, self.manager)
        report = engine.recover()
        print(f"Recovery: {report}")

    def _start_health_monitor(self) -> None:
        from flint.core._health import HealthMonitor
        self._health_monitor = HealthMonitor(
            state_store=self._state_store,
            manager=self.manager,
            interval=HEALTH_CHECK_INTERVAL,
        )
        self._health_monitor.start()
        print("Health monitor started.")

    def _start_lifecycle(self) -> None:
        from flint.core._lifecycle import LifecycleManager
        self._lifecycle_manager = LifecycleManager(
            state_store=self._state_store,
            manager=self.manager,
            interval=1.0,
            error_cleanup_delay=ERROR_CLEANUP_DELAY,
        )
        self._lifecycle_manager.start()
        print("Lifecycle manager started.")

    def _install_signal_handlers(self) -> None:
        def _shutdown(signum, frame):
            print("\nShutting down (VMs will keep running)...")
            if self._health_monitor:
                self._health_monitor.stop()
            if self._lifecycle_manager:
                self._lifecycle_manager.stop()
            if self.cluster:
                self.cluster.stop()
            # Detach from VMs without killing them — they'll be recovered on restart
            if self.manager:
                with self.manager._lock:
                    self.manager._sandboxes.clear()
            if self.storage:
                self.storage.stop()
            self.backend.stop_pool()
            if self._state_store:
                self._state_store.close()
            for path in (DAEMON_STATE_PATH, DAEMON_PID_PATH, DAEMON_SOCKET):
                try:
                    os.unlink(path)
                except OSError:
                    pass
            raise SystemExit(0)

        signal.signal(signal.SIGINT, _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)

    def _serve(self) -> None:
        if self.cluster:
            self._serve_cluster()
            return
        sockets = [_listen_unix(DAEMON_SOCKET), _listen_tcp(DAEMON_HOST, DAEMON_PORT)]
        print(f"Daemon ready on {DAEMON_SOCKET} and http://{DAEMON_HOST}:{DAEMON_PORT} (API token required; "
              f"admin token in {ADMIN_TOKEN_PATH}) · web UI: `flint ui`")
        # The timeout is a backstop: a stuck handler mustn't keep the daemon alive.
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", timeout_graceful_shutdown=5))

        # On macOS, the Virtualization.framework needs the main thread's RunLoop
        # to drive VM execution.  Run uvicorn on a background thread and reserve
        # the main thread for the VZ backend's RunLoop.
        if self.backend.kind == "macos-vz-arm64":
            from flint.core.backends.macos_vz import MacOSVirtualizationBackend

            if isinstance(self.backend, MacOSVirtualizationBackend):
                threading.Thread(target=lambda: server.run(sockets=sockets), daemon=True).start()
                self.backend._runtime.run_loop()  # blocks forever
                return

        server.run(sockets=sockets)

    def _serve_cluster(self) -> None:
        """Serve over mTLS: only the control plane's certificates get in.
        The host certificate is reloaded when snapd renews it."""
        cfg = uvicorn.Config(app, host=DAEMON_HOST, port=DAEMON_PORT, log_level="warning",
                             timeout_graceful_shutdown=5, **self.cluster.server_ssl())
        cfg.load()
        ctx, files = cfg.ssl, self.cluster.server_ssl()
        mtime = [os.path.getmtime(files["ssl_certfile"])]

        def _reload_cert():
            while True:
                time.sleep(30)
                try:
                    m = os.path.getmtime(files["ssl_certfile"])
                    if m != mtime[0]:
                        ctx.load_cert_chain(files["ssl_certfile"], files["ssl_keyfile"])
                        mtime[0] = m
                        log.info("cluster: reloaded host certificate")
                except Exception:
                    log.exception("cluster: certificate reload failed")

        threading.Thread(target=_reload_cert, daemon=True, name="cert-reload").start()
        uvicorn.Server(cfg).run()

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import threading
import time
import urllib.parse

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request, Response
import uvicorn

from flint.core.backends import get_host_backend
from flint.core.config import (
    log, DAEMON_HOST, DAEMON_PORT, DAEMON_DIR, DAEMON_STATE_PATH, DAEMON_PID_PATH,
    TEMPLATES_DIR, DEFAULT_TEMPLATE_ID,
    DAEMON_DB_PATH, HEALTH_CHECK_INTERVAL, ERROR_CLEANUP_DELAY,
    WORKSPACE_DIR,
)
from flint.core.manager import SandboxManager
from flint.core._template_registry import (
    list_templates, get_template, delete_template as _delete_template,
)
from flint.core._volumes import VolumeStore
from flint.core._metrics import MetricsSampler
from flint.core import _logs
from flint.daemon import events as _events

app = FastAPI()


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


def _write_state(daemon: FlintDaemon) -> None:
    """Atomically write daemon state to JSON file."""
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
    os.rename(tmp_path, DAEMON_STATE_PATH)


async def _agent_request_async(vm_id: str, method: str, path: str, body: bytes | None = None, timeout: float = 65) -> tuple[int, bytes]:
    """Async wrapper: runs backend transport in a worker thread."""
    mgr = _require_manager()
    _require_entry(vm_id)
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, mgr.agent_request, vm_id, method, path, body, timeout)


# ── Endpoints ──────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    print("GET /health")
    daemon = _get_daemon()
    result = {"status": "ok", "golden_snapshot_ready": daemon.golden_ready, "backend_kind": daemon.backend.kind}
    if daemon.storage:
        result["storage_backend"] = daemon.storage.kind
        result["storage_healthy"] = daemon.storage.is_running()
    return result


@app.post("/vms")
async def create_vm(request: Request, template_id: str = DEFAULT_TEMPLATE_ID, allow_internet_access: bool = True, use_pool: bool = True, use_pyroute2: bool = True):
    print(f"POST /vms — creating VM (template={template_id}, internet={allow_internet_access})...")
    daemon = _get_daemon()
    mgr = _require_manager()
    if template_id == DEFAULT_TEMPLATE_ID and not daemon.golden_ready:
        raise HTTPException(status_code=503, detail="Golden snapshot not ready")
    # Parse optional network_policy from request body
    network_policy = None
    body = await request.body()
    if body:
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            raise HTTPException(status_code=400, detail="Invalid JSON in request body")
        network_policy = data.get("network_policy")
        if network_policy is not None:
            _validate_network_policy(network_policy)

    # Run in executor to avoid blocking the event loop (create can take 10s+ on failure)
    loop = asyncio.get_event_loop()
    try:
        vm_id = await loop.run_in_executor(
            None,
            lambda: mgr.create(
                template_id=template_id,
                allow_internet_access=allow_internet_access,
                use_pool=use_pool,
                use_pyroute2=use_pyroute2,
                network_policy=network_policy,
            ),
        )
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

    result = mgr.get_dict(vm_id) or {"vm_id": vm_id}
    if daemon.storage:
        result["storage_backend"] = daemon.storage.kind
        result["workspace_dir"] = WORKSPACE_DIR
    _write_state(daemon)
    _events.publish("vm.created", vm=result)
    _logs.append(vm_id, f"VM created from template '{template_id}' in {result.get('ready_time_ms', 0):.0f} ms")
    for step, ms in (result.get("timings") or {}).items():
        _logs.append(vm_id, f"  timing.{step} = {ms:.1f} ms")
    print(f"POST /vms — created {vm_id[:8]}")
    return {"vm": result}


@app.get("/vms")
def list_vms():
    daemon = _get_daemon()
    if not daemon.manager:
        return {"vms": []}
    entries = daemon.manager.list_dicts()
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
    entry_dict = mgr.get_dict(vm_id)
    if entry_dict is None:
        print(f"DELETE /vms/{vm_id[:8]} — not found")
        raise HTTPException(status_code=404, detail="VM not found")
    # Tear down storage before killing the VM (no-op for local backend).
    if daemon.storage:
        entry = mgr.get_entry(vm_id)
        veth_ip = ""
        if entry:
            veth_ip = (entry.backend_metadata or {}).get("veth_ip", "")
        daemon.storage.teardown_sandbox(vm_id, veth_ip)
    mgr.kill(vm_id)
    _write_state(daemon)
    _events.publish("vm.deleted", vm_id=vm_id)
    _logs.append(vm_id, "VM killed")
    print(f"DELETE /vms/{vm_id[:8]} — killed")
    return {"ok": True}


@app.post("/vms/{vm_id}/pause")
def pause_vm(vm_id: str):
    print(f"POST /vms/{vm_id[:8]}/pause")
    daemon = _get_daemon()
    mgr = _require_manager()
    try:
        mgr.pause(vm_id)
    except Exception as e:
        log.exception("POST /vms/%s/pause — failed", vm_id[:8])
        print(f"POST /vms/{vm_id[:8]}/pause — failed: {e}")
        raise HTTPException(status_code=500, detail=f"Pause failed: {e}")
    _write_state(daemon)
    _events.publish("vm.paused", vm_id=vm_id)
    _logs.append(vm_id, "VM paused")
    print(f"POST /vms/{vm_id[:8]}/pause — paused")
    return {"ok": True}


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
    result = mgr.get_dict(vm_id) or {"vm_id": vm_id}
    _write_state(daemon)
    _events.publish("vm.resumed", vm=result)
    _logs.append(vm_id, "VM resumed")
    print(f"POST /vms/{vm_id[:8]}/resume — resumed")
    return {"vm": result}


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
    mgr.update_network_policy(vm_id, body)
    print(f"PUT /vms/{vm_id[:8]}/network-policy — updated")
    return {"ok": True}


@app.get("/vms/{vm_id}/network-policy")
def get_network_policy(vm_id: str):
    """Get the current network policy for a sandbox."""
    mgr = _require_manager()
    _require_entry(vm_id)
    return {"network_policy": mgr.get_network_policy(vm_id)}


# ── Guest agent proxy endpoints ───────────────────────────────────────────

@app.post("/vms/{vm_id}/exec")
async def exec_command(vm_id: str, request: Request):
    """Proxy POST /exec to the guest agent."""
    print(f"POST /vms/{vm_id[:8]}/exec")
    body = await request.body()
    status, resp_body = await _agent_request_async(vm_id, "POST", "/exec", body)
    return Response(content=resp_body, status_code=status, media_type="application/json")


@app.post("/vms/{vm_id}/processes")
async def create_process(vm_id: str, request: Request):
    """Proxy POST /processes to the guest agent."""
    print(f"POST /vms/{vm_id[:8]}/processes")
    body = await request.body()
    status, resp_body = await _agent_request_async(vm_id, "POST", "/processes", body)
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

@app.websocket("/events")
async def events_ws(websocket: WebSocket):
    """Stream daemon-wide lifecycle events to a subscriber."""
    bus = _events.get_bus()
    if bus is None:
        await websocket.close(code=1011, reason="Event bus not initialized")
        return
    await websocket.accept()
    q = bus.subscribe()
    try:
        while True:
            event = await q.get()
            await websocket.send_json(event)
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
        while True:
            line = await q.get()
            await websocket.send_text(line)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("WS /vms/%s/logs — error", vm_id[:8])
    finally:
        bus.unsubscribe(vm_id, q)


# ── Template endpoints ──────────────────────────────────────────────────────

_build_threads: dict[str, threading.Thread] = {}


@app.post("/templates/build")
def build_template_endpoint(body: dict):
    name = body.get("name")
    dockerfile = body.get("dockerfile")
    rootfs_size_mb = body.get("rootfs_size_mb", 500)
    if not name or not dockerfile:
        raise HTTPException(status_code=400, detail="name and dockerfile are required")

    from flint.core._template_build import _slugify
    template_id = _slugify(name)
    print(f"POST /templates/build — building {template_id}...")

    daemon = _get_daemon()

    def _run_build():
        try:
            daemon.backend.build_template(name, dockerfile, rootfs_size_mb=rootfs_size_mb)
            print(f"Template {template_id} built successfully")
        except Exception as e:
            print(f"Template {template_id} build failed: {e}")
            log.exception("Template build failed: %s", template_id)

    t = threading.Thread(target=_run_build, daemon=True, name=f"build-{template_id}")
    t.start()
    _build_threads[template_id] = t

    return {"template_id": template_id, "status": "building"}


@app.get("/templates")
def list_templates_endpoint():
    templates = list_templates()
    print(f"GET /templates — {len(templates)} templates")
    return {"templates": templates}


@app.get("/templates/{template_id}")
def get_template_endpoint(template_id: str):
    tmpl = get_template(template_id)
    if tmpl is None:
        raise HTTPException(status_code=404, detail="Template not found")
    return {"template": tmpl}


@app.delete("/templates/{template_id}")
def delete_template_endpoint(template_id: str):
    print(f"DELETE /templates/{template_id}")
    if template_id == DEFAULT_TEMPLATE_ID:
        raise HTTPException(status_code=400, detail="Cannot delete default template")
    tmpl = get_template(template_id)
    if tmpl is None:
        raise HTTPException(status_code=404, detail="Template not found")
    daemon = _get_daemon()
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

def _require_volumes() -> VolumeStore:
    daemon = _get_daemon()
    if daemon.volumes is None:
        raise HTTPException(status_code=503, detail="Volume store not initialized")
    return daemon.volumes


@app.get("/volumes")
def list_volumes():
    vols = _require_volumes().list()
    print(f"GET /volumes — {len(vols)} volumes")
    return {"volumes": [v.to_dict() for v in vols]}


@app.post("/volumes")
def create_volume(body: dict):
    name = body.get("name")
    size_gib = body.get("size_gib")
    if not isinstance(name, str) or not name:
        raise HTTPException(status_code=400, detail="'name' is required")
    if not isinstance(size_gib, int):
        raise HTTPException(status_code=400, detail="'size_gib' must be an integer")
    try:
        vol = _require_volumes().create(name, size_gib)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _events.publish("volume.created", volume=vol.to_dict())
    print(f"POST /volumes — created {vol.id} ({name}, {size_gib} GiB)")
    return {"volume": vol.to_dict()}


@app.delete("/volumes/{volume_id}")
def delete_volume(volume_id: str):
    ok = _require_volumes().delete(volume_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Volume not found")
    _events.publish("volume.deleted", volume_id=volume_id)
    print(f"DELETE /volumes/{volume_id} — deleted")
    return {"ok": True}


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
        self.volumes: VolumeStore | None = None
        self.metrics: MetricsSampler | None = None

    def run(self) -> None:
        self.started_at = time.time()
        app.state.daemon = self

        self._init_dirs()
        self._write_pid()
        self._prepare_backend()
        self._init_storage()
        self._start_pool()
        self._init_state_store()
        self._init_manager()
        self._recover_sandboxes()
        self._start_health_monitor()
        self._start_lifecycle()
        self._install_signal_handlers()
        self._serve()

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

    def _init_storage(self) -> None:
        from flint.core.storage import get_storage_backend
        self.storage = get_storage_backend()
        self.storage.start()
        print(f"Storage backend: {self.storage.kind}")

    def _start_pool(self) -> None:
        self.backend.start_pool()
        print("Backend pool started.")

    def _init_state_store(self) -> None:
        from flint.core._state_store import StateStore
        self._state_store = StateStore(DAEMON_DB_PATH)
        self.volumes = VolumeStore(self._state_store._conn)
        print("State store initialized.")

    def _init_manager(self) -> None:
        self.manager = SandboxManager(backend=self.backend, state_store=self._state_store)
        self.metrics = MetricsSampler(lambda: list(self.manager._sandboxes.values()) if self.manager else [])
        _write_state(self)

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
            # Detach from VMs without killing them — they'll be recovered on restart
            if self.manager:
                with self.manager._lock:
                    self.manager._sandboxes.clear()
            if self.storage:
                self.storage.stop()
            self.backend.stop_pool()
            if self._state_store:
                self._state_store.close()
            for path in (DAEMON_STATE_PATH, DAEMON_PID_PATH):
                try:
                    os.unlink(path)
                except OSError:
                    pass
            raise SystemExit(0)

        signal.signal(signal.SIGINT, _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)

    def _serve(self) -> None:
        print(f"Daemon ready on {DAEMON_HOST}:{DAEMON_PORT}")

        # On macOS, the Virtualization.framework needs the main thread's RunLoop
        # to drive VM execution.  Run uvicorn on a background thread and reserve
        # the main thread for the VZ backend's RunLoop.
        if self.backend.kind == "macos-vz-arm64":
            from flint.core.backends.macos_vz import MacOSVirtualizationBackend

            if isinstance(self.backend, MacOSVirtualizationBackend):
                uvicorn_thread = threading.Thread(
                    target=lambda: uvicorn.run(
                        app, host=DAEMON_HOST, port=DAEMON_PORT, log_level="warning",
                    ),
                    daemon=True,
                )
                uvicorn_thread.start()
                self.backend._runtime.run_loop()  # blocks forever
                return

        uvicorn.run(app, host=DAEMON_HOST, port=DAEMON_PORT, log_level="warning")

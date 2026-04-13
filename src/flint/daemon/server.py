from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import signal
import threading
import time

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request, Response
from fastapi.responses import JSONResponse
import uvicorn

from flint.core.backends import (
    available_backends,
    default_backend_kind,
    get_backend,
)
from flint.core.backends.capabilities import (
    EvalRequest,
    ExecRequest,
    FetchRequest,
)
from flint.core.config import (
    log, DAEMON_HOST, DAEMON_PORT, DAEMON_DIR, DAEMON_STATE_PATH, DAEMON_PID_PATH,
    TEMPLATES_DIR, DEFAULT_TEMPLATE_ID,
    DAEMON_DB_PATH, HEALTH_CHECK_INTERVAL, ERROR_CLEANUP_DELAY,
)
from flint.core.manager import SandboxManager
from flint.core._template_registry import (
    list_templates, get_template, delete_template as _delete_template,
)
from flint.errors import BackendCapabilityMissing

app = FastAPI()


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


@app.exception_handler(BackendCapabilityMissing)
async def _capability_missing_handler(request: Request, exc: BackendCapabilityMissing):
    return JSONResponse(
        status_code=409,
        content={
            "error": "capability_missing",
            "capability": exc.capability,
            "backend": exc.backend,
            "detail": str(exc),
        },
    )


def _validate_network_policy(policy: object) -> None:
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


def _run_in_thread(fn, *args, **kwargs):
    """Run a blocking call on a thread; capability/HTTP errors propagate."""
    loop = asyncio.get_event_loop()
    return loop.run_in_executor(None, lambda: fn(*args, **kwargs))


# ── Lifecycle endpoints ────────────────────────────────────────────────────

@app.get("/health")
def health():
    daemon = _get_daemon()
    return {
        "status": "ok",
        "golden_snapshot_ready": daemon.golden_ready,
        "default_backend": daemon.default_backend_kind,
        "available_backends": available_backends(),
    }


@app.get("/backends")
def list_backends():
    out = []
    for kind in available_backends():
        try:
            b = get_backend(kind)
            out.append({"kind": kind, "capabilities": sorted(b.capabilities)})
        except Exception as exc:
            out.append({"kind": kind, "error": str(exc)})
    return {"backends": out, "default": _get_daemon().default_backend_kind}


@app.post("/vms")
async def create_vm(request: Request, template_id: str = DEFAULT_TEMPLATE_ID):
    daemon = _get_daemon()
    mgr = _require_manager()
    if template_id == DEFAULT_TEMPLATE_ID and not daemon.golden_ready:
        raise HTTPException(status_code=503, detail="Golden snapshot not ready")

    backend_kind: str | None = None
    options: dict = {}
    network_policy = None

    raw = await request.body()
    if raw:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            raise HTTPException(status_code=400, detail="Invalid JSON in request body")
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="Request body must be a JSON object")
        backend_kind = data.get("backend")
        opts = data.get("options")
        if opts is not None:
            if not isinstance(opts, dict):
                raise HTTPException(status_code=400, detail="'options' must be a JSON object")
            options = opts
        network_policy = data.get("network_policy")
        if network_policy is not None:
            _validate_network_policy(network_policy)

    print(f"POST /vms (backend={backend_kind or 'default'}, template={template_id})")
    vm_id = mgr.create(
        backend=backend_kind,
        template_id=template_id,
        options=options,
        network_policy=network_policy,
    )
    result = mgr.get_dict(vm_id) or {"vm_id": vm_id}
    _write_state(daemon)
    print(f"POST /vms — created {vm_id[:8]}")
    return {"vm": result}


@app.get("/vms")
def list_vms():
    daemon = _get_daemon()
    if not daemon.manager:
        return {"vms": []}
    return {"vms": daemon.manager.list_dicts()}


@app.get("/vms/{vm_id}")
def get_vm(vm_id: str):
    mgr = _require_manager()
    result = mgr.get_dict(vm_id)
    if not result:
        raise HTTPException(status_code=404, detail="VM not found")
    return {"vm": result}


@app.delete("/vms/{vm_id}")
def delete_vm(vm_id: str):
    daemon = _get_daemon()
    mgr = _require_manager()
    if mgr.get_dict(vm_id) is None:
        raise HTTPException(status_code=404, detail="VM not found")
    mgr.kill(vm_id)
    _write_state(daemon)
    return {"ok": True}


@app.post("/vms/{vm_id}/pause")
def pause_vm(vm_id: str):
    daemon = _get_daemon()
    mgr = _require_manager()
    mgr.pause(vm_id)
    _write_state(daemon)
    return {"ok": True}


@app.post("/vms/{vm_id}/resume")
def resume_vm(vm_id: str):
    daemon = _get_daemon()
    mgr = _require_manager()
    mgr.resume(vm_id)
    result = mgr.get_dict(vm_id) or {"vm_id": vm_id}
    _write_state(daemon)
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


# ── Network policy ────────────────────────────────────────────────────────

@app.put("/vms/{vm_id}/network-policy")
async def update_network_policy(vm_id: str, request: Request):
    mgr = _require_manager()
    _require_entry(vm_id)
    try:
        body = json.loads(await request.body())
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid JSON in request body")
    _validate_network_policy(body)
    mgr.update_network_policy(vm_id, body)
    return {"ok": True}


@app.get("/vms/{vm_id}/network-policy")
def get_network_policy(vm_id: str):
    mgr = _require_manager()
    _require_entry(vm_id)
    return {"network_policy": mgr.get_network_policy(vm_id)}


# ── Shell capability ──────────────────────────────────────────────────────

@app.post("/vms/{vm_id}/exec")
async def exec_command(vm_id: str, request: Request):
    mgr = _require_manager()
    _require_entry(vm_id)
    try:
        body = json.loads(await request.body() or b"{}")
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid JSON in request body")
    cmd = body.get("cmd")
    if not isinstance(cmd, list) or not all(isinstance(x, str) for x in cmd):
        raise HTTPException(status_code=400, detail="'cmd' must be a list of strings")
    req = ExecRequest(
        cmd=cmd,
        env=body.get("env"),
        cwd=body.get("cwd"),
        timeout=float(body.get("timeout", 60)),
    )
    result = await _run_in_thread(mgr.exec, vm_id, req)
    return {"stdout": result.stdout, "stderr": result.stderr, "exit_code": result.exit_code}


# ── PTY / Process capability ──────────────────────────────────────────────

@app.post("/vms/{vm_id}/processes")
async def create_process(vm_id: str, request: Request):
    from flint.core.backends.capabilities import ProcessSpec
    mgr = _require_manager()
    _require_entry(vm_id)
    try:
        body = json.loads(await request.body() or b"{}")
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid JSON")
    spec = ProcessSpec(
        cmd=body.get("cmd", []),
        pty=bool(body.get("pty", False)),
        cols=int(body.get("cols", 120)),
        rows=int(body.get("rows", 40)),
        env=body.get("env"),
        cwd=body.get("cwd"),
    )
    handle = await _run_in_thread(mgr.create_process, vm_id, spec)
    return {"pid": handle.pid}


@app.get("/vms/{vm_id}/processes")
async def list_processes(vm_id: str):
    mgr = _require_manager()
    _require_entry(vm_id)
    procs = await _run_in_thread(mgr.list_processes, vm_id)
    return {"processes": procs}


@app.post("/vms/{vm_id}/processes/{pid}/input")
async def process_input(vm_id: str, pid: int, request: Request):
    mgr = _require_manager()
    _require_entry(vm_id)
    body = await request.body()
    await _run_in_thread(mgr.send_process_input, vm_id, pid, body)
    return {"ok": True}


@app.post("/vms/{vm_id}/processes/{pid}/signal")
async def process_signal(vm_id: str, pid: int, request: Request):
    mgr = _require_manager()
    _require_entry(vm_id)
    try:
        body = json.loads(await request.body() or b"{}")
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid JSON")
    sig = int(body.get("signal", 15))
    await _run_in_thread(mgr.signal_process, vm_id, pid, sig)
    return {"ok": True}


@app.post("/vms/{vm_id}/processes/{pid}/resize")
async def process_resize(vm_id: str, pid: int, request: Request):
    mgr = _require_manager()
    _require_entry(vm_id)
    try:
        body = json.loads(await request.body() or b"{}")
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid JSON")
    cols = int(body.get("cols", 120))
    rows = int(body.get("rows", 40))
    await _run_in_thread(mgr.resize_process, vm_id, pid, cols, rows)
    return {"ok": True}


@app.websocket("/vms/{vm_id}/terminal")
async def terminal_ws(websocket: WebSocket, vm_id: str):
    daemon = _get_daemon()
    if not daemon.manager:
        await websocket.close(code=1011, reason="Manager not initialized")
        return
    try:
        await daemon.manager.attach_terminal(vm_id, websocket)
    except BackendCapabilityMissing as exc:
        await websocket.close(code=1011, reason=f"capability {exc.capability!r} not supported")
    except RuntimeError:
        await websocket.close(code=1011, reason="VM not found")
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("WS /vms/%s/terminal — error", vm_id[:8])


# ── Files capability ──────────────────────────────────────────────────────

@app.get("/vms/{vm_id}/files")
async def read_file(vm_id: str, path: str):
    mgr = _require_manager()
    _require_entry(vm_id)
    data = await _run_in_thread(mgr.read_file, vm_id, path)
    return Response(content=data, status_code=200, media_type="application/octet-stream")


@app.post("/vms/{vm_id}/files")
async def write_file(vm_id: str, path: str, request: Request, mode: str = "0644"):
    mgr = _require_manager()
    _require_entry(vm_id)
    body = await request.body()
    await _run_in_thread(mgr.write_file, vm_id, path, body, mode)
    return {"ok": True}


@app.get("/vms/{vm_id}/files/stat")
async def stat_file(vm_id: str, path: str):
    mgr = _require_manager()
    _require_entry(vm_id)
    info = await _run_in_thread(mgr.stat_file, vm_id, path)
    return {
        "name": info.name, "path": info.path, "size": info.size,
        "is_dir": info.is_dir, "mode": info.mode, "modified_at": info.modified_at,
    }


@app.get("/vms/{vm_id}/files/list")
async def list_files(vm_id: str, path: str):
    mgr = _require_manager()
    _require_entry(vm_id)
    items = await _run_in_thread(mgr.list_files, vm_id, path)
    return {
        "entries": [
            {"name": i.name, "path": i.path, "size": i.size, "is_dir": i.is_dir,
             "mode": i.mode, "modified_at": i.modified_at}
            for i in items
        ]
    }


@app.post("/vms/{vm_id}/files/mkdir")
async def mkdir(vm_id: str, path: str, parents: bool = True):
    mgr = _require_manager()
    _require_entry(vm_id)
    await _run_in_thread(mgr.mkdir, vm_id, path, parents)
    return {"ok": True}


@app.delete("/vms/{vm_id}/files")
async def delete_file(vm_id: str, path: str, recursive: bool = False):
    mgr = _require_manager()
    _require_entry(vm_id)
    await _run_in_thread(mgr.delete_file, vm_id, path, recursive)
    return {"ok": True}


# ── JS / fetch / kv capabilities (used by the V8 backend in stage B) ──────

@app.post("/vms/{vm_id}/eval")
async def eval_js(vm_id: str, request: Request):
    mgr = _require_manager()
    _require_entry(vm_id)
    try:
        body = json.loads(await request.body() or b"{}")
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid JSON")
    req = EvalRequest(code=body.get("code", ""), timeout=float(body.get("timeout", 30)))
    result = await _run_in_thread(mgr.eval_js, vm_id, req)
    return {"result": result.result, "stdout": result.stdout, "stderr": result.stderr, "error": result.error}


@app.post("/vms/{vm_id}/fetch")
async def proxy_fetch(vm_id: str, request: Request):
    mgr = _require_manager()
    _require_entry(vm_id)
    try:
        body = json.loads(await request.body() or b"{}")
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid JSON")
    raw_body = None
    if body.get("body_b64"):
        raw_body = base64.b64decode(body["body_b64"])
    req = FetchRequest(
        method=body.get("method", "GET"),
        url=body.get("url", ""),
        headers=body.get("headers", {}),
        body=raw_body,
    )
    resp = await _run_in_thread(mgr.fetch, vm_id, req)
    return {
        "status": resp.status,
        "headers": resp.headers,
        "body_b64": base64.b64encode(resp.body).decode(),
    }


@app.get("/vms/{vm_id}/kv/{key}")
async def kv_get_endpoint(vm_id: str, key: str):
    mgr = _require_manager()
    _require_entry(vm_id)
    value = await _run_in_thread(mgr.kv_get, vm_id, key)
    if value is None:
        raise HTTPException(status_code=404, detail="key not found")
    return Response(content=value, status_code=200, media_type="application/octet-stream")


@app.put("/vms/{vm_id}/kv/{key}")
async def kv_put_endpoint(vm_id: str, key: str, request: Request):
    mgr = _require_manager()
    _require_entry(vm_id)
    body = await request.body()
    await _run_in_thread(mgr.kv_put, vm_id, key, body)
    return {"ok": True}


@app.delete("/vms/{vm_id}/kv/{key}")
async def kv_delete_endpoint(vm_id: str, key: str):
    mgr = _require_manager()
    _require_entry(vm_id)
    await _run_in_thread(mgr.kv_delete, vm_id, key)
    return {"ok": True}


# ── Templates ──────────────────────────────────────────────────────────────

_build_threads: dict[str, threading.Thread] = {}


@app.post("/templates/build")
def build_template_endpoint(body: dict):
    name = body.get("name")
    dockerfile = body.get("dockerfile")
    rootfs_size_mb = body.get("rootfs_size_mb", 500)
    backend_kind = body.get("backend") or _get_daemon().default_backend_kind
    if not name or not dockerfile:
        raise HTTPException(status_code=400, detail="name and dockerfile are required")

    from flint.core._template_build import _slugify
    template_id = _slugify(name)

    mgr = _require_manager()
    builder = mgr.template_builder_for(backend_kind)

    def _run_build():
        try:
            builder.build_template(name, dockerfile, rootfs_size_mb=rootfs_size_mb)
            print(f"Template {template_id} built successfully")
        except Exception:
            log.exception("Template build failed: %s", template_id)

    t = threading.Thread(target=_run_build, daemon=True, name=f"build-{template_id}")
    t.start()
    _build_threads[template_id] = t

    return {"template_id": template_id, "status": "building", "backend": backend_kind}


@app.get("/templates")
def list_templates_endpoint():
    return {"templates": list_templates()}


@app.get("/templates/{template_id}")
def get_template_endpoint(template_id: str):
    tmpl = get_template(template_id)
    if tmpl is None:
        raise HTTPException(status_code=404, detail="Template not found")
    return {"template": tmpl}


@app.delete("/templates/{template_id}")
def delete_template_endpoint(template_id: str):
    if template_id == DEFAULT_TEMPLATE_ID:
        raise HTTPException(status_code=400, detail="Cannot delete default template")
    tmpl = get_template(template_id)
    if tmpl is None:
        raise HTTPException(status_code=404, detail="Template not found")
    mgr = _require_manager()
    artifacts = tmpl.get("artifacts") or {}
    for backend_kind in artifacts.keys():
        try:
            builder = mgr.template_builder_for(backend_kind)
            builder.delete_template_artifact(template_id, tmpl)
        except BackendCapabilityMissing:
            log.warning("Backend %s does not support template_build, skipping artifact cleanup", backend_kind)
        except Exception:
            log.exception("Failed to delete template artifact for %s/%s", template_id, backend_kind)
    for artifact in artifacts.values():
        tdir = artifact.get("template_dir", "")
        if tdir and os.path.isdir(tdir):
            shutil.rmtree(tdir, ignore_errors=True)
    _delete_template(template_id)
    return {"ok": True}


# ── FlintDaemon ──────────────────────────────────────────────────────────

class FlintDaemon:
    def __init__(self) -> None:
        self.default_backend_kind: str = default_backend_kind()
        self.manager: SandboxManager | None = None
        self.golden_ready: bool = False
        self.started_at: float = 0.0
        self._state_store = None
        self._health_monitor = None
        self._lifecycle_manager = None

    def run(self) -> None:
        self.started_at = time.time()
        app.state.daemon = self

        self._init_dirs()
        self._write_pid()
        self._init_state_store()
        self._init_manager()
        self._prepare_default_backend()
        self._start_pools()
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

    def _prepare_default_backend(self) -> None:
        os.makedirs(TEMPLATES_DIR, exist_ok=True)
        assert self.manager is not None
        backend = self.manager.backend_for(self.default_backend_kind)
        backend.ensure_runtime_ready()
        backend.ensure_default_template()
        default_template = get_template(DEFAULT_TEMPLATE_ID) or {}
        artifact = (default_template.get("artifacts") or {}).get(backend.kind) or {}
        self.golden_ready = artifact.get("status") == "ready"
        print(f"Default backend ready: {backend.kind}")

    def _start_pools(self) -> None:
        assert self.manager is not None
        self.manager.start_pools()

    def _init_state_store(self) -> None:
        from flint.core._state_store import StateStore
        self._state_store = StateStore(DAEMON_DB_PATH)

    def _init_manager(self) -> None:
        self.manager = SandboxManager(state_store=self._state_store)
        self.manager.default_kind = self.default_backend_kind
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

    def _start_lifecycle(self) -> None:
        from flint.core._lifecycle import LifecycleManager
        self._lifecycle_manager = LifecycleManager(
            state_store=self._state_store,
            manager=self.manager,
            interval=1.0,
            error_cleanup_delay=ERROR_CLEANUP_DELAY,
        )
        self._lifecycle_manager.start()

    def _install_signal_handlers(self) -> None:
        def _shutdown(signum, frame):
            print("\nShutting down (VMs will keep running)...")
            if self._health_monitor:
                self._health_monitor.stop()
            if self._lifecycle_manager:
                self._lifecycle_manager.stop()
            if self.manager:
                with self.manager._lock:
                    self.manager._sandboxes.clear()
                self.manager.stop_pools()
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

        # On macOS, the Virtualization.framework needs the main thread's RunLoop.
        if self.default_backend_kind == "macos-vz-arm64":
            from flint.core.backends.macos_vz import MacOSVirtualizationBackend
            assert self.manager is not None
            backend = self.manager.backend_for(self.default_backend_kind)
            if isinstance(backend, MacOSVirtualizationBackend):
                uvicorn_thread = threading.Thread(
                    target=lambda: uvicorn.run(
                        app, host=DAEMON_HOST, port=DAEMON_PORT, log_level="warning",
                    ),
                    daemon=True,
                )
                uvicorn_thread.start()
                backend._runtime.run_loop()  # blocks forever
                return

        uvicorn.run(app, host=DAEMON_HOST, port=DAEMON_PORT, log_level="warning")

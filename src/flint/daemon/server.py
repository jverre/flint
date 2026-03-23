from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import threading
import time
import urllib.parse
import urllib.request

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request, Response
import uvicorn

from flint.core.config import (
    log, DAEMON_HOST, DAEMON_PORT, DAEMON_DIR, DAEMON_STATE_PATH, DAEMON_PID_PATH,
    GOLDEN_DIR, TEMPLATES_DIR, DEFAULT_TEMPLATE_ID,
    DAEMON_DB_PATH, HEALTH_CHECK_INTERVAL, DEFAULT_SANDBOX_TIMEOUT, ERROR_CLEANUP_DELAY,
)
from flint.core.manager import SandboxManager
from flint.core._snapshot import create_golden_snapshot, golden_snapshot_exists
from flint.core._pool import start_pool, stop_pool
from flint.core._netns import _ensure_bridge, _enter_netns, _restore_netns
from flint.core._template_registry import (
    list_templates, get_template, register_template, delete_template as _delete_template,
    get_template_dir,
)
from flint.core._template_build import build_template as _build_template

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


def _agent_request(entry, method: str, path: str, body: bytes | None = None, timeout: float = 65) -> tuple[int, bytes]:
    """Make an HTTP request to the guest agent inside the VM's network namespace.

    IMPORTANT: Uses setns which only affects the calling kernel thread.
    Safe to call from worker threads but NOT from the async event loop thread directly.
    For async endpoints, use _agent_request_async instead.
    """
    url = f"{entry.agent_url}{path}"
    req = urllib.request.Request(url, data=body, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")

    orig_fd = _enter_netns(entry.ns_name)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    finally:
        _restore_netns(orig_fd)


async def _agent_request_async(entry, method: str, path: str, body: bytes | None = None, timeout: float = 65) -> tuple[int, bytes]:
    """Async wrapper: runs _agent_request in a thread pool to avoid setns on the event loop thread."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _agent_request, entry, method, path, body, timeout)


# ── Endpoints ──────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    print("GET /health")
    daemon = _get_daemon()
    return {"status": "ok", "golden_snapshot_ready": daemon.golden_ready}


@app.post("/vms")
async def create_vm(request: Request, template_id: str = DEFAULT_TEMPLATE_ID, allow_internet_access: bool = True, use_pool: bool = True, use_pyroute2: bool = True):
    print(f"POST /vms — creating VM (template={template_id}, internet={allow_internet_access})...")
    daemon = _get_daemon()
    mgr = _require_manager()
    if template_id == DEFAULT_TEMPLATE_ID and not daemon.golden_ready:
        raise HTTPException(status_code=503, detail="Golden snapshot not ready")
    # Parse optional network_policy from request body
    network_policy = None
    try:
        body = await request.body()
        if body:
            data = json.loads(body)
            network_policy = data.get("network_policy")
    except Exception:
        pass
    vm_id = mgr.create(template_id=template_id, allow_internet_access=allow_internet_access, use_pool=use_pool, use_pyroute2=use_pyroute2, network_policy=network_policy)
    result = mgr.get_dict(vm_id) or {"vm_id": vm_id}
    _write_state(daemon)
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
    if mgr.get_dict(vm_id) is None:
        print(f"DELETE /vms/{vm_id[:8]} — not found")
        raise HTTPException(status_code=404, detail="VM not found")
    mgr.kill(vm_id)
    _write_state(daemon)
    print(f"DELETE /vms/{vm_id[:8]} — killed")
    return {"ok": True}


@app.post("/vms/{vm_id}/pause")
def pause_vm(vm_id: str):
    print(f"POST /vms/{vm_id[:8]}/pause")
    daemon = _get_daemon()
    mgr = _require_manager()
    mgr.pause(vm_id)
    _write_state(daemon)
    print(f"POST /vms/{vm_id[:8]}/pause — paused")
    return {"ok": True}


@app.post("/vms/{vm_id}/resume")
def resume_vm(vm_id: str):
    print(f"POST /vms/{vm_id[:8]}/resume")
    daemon = _get_daemon()
    mgr = _require_manager()
    mgr.resume(vm_id)
    result = mgr.get_dict(vm_id) or {"vm_id": vm_id}
    _write_state(daemon)
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


# ── Network policy endpoints ──────────────────────────────────────────────

@app.put("/vms/{vm_id}/network-policy")
async def update_network_policy(vm_id: str, request: Request):
    """Update the network policy (credential injection rules) for a sandbox."""
    print(f"PUT /vms/{vm_id[:8]}/network-policy")
    mgr = _require_manager()
    entry = _require_entry(vm_id)
    body = json.loads(await request.body())
    mgr.update_network_policy(vm_id, body)
    print(f"PUT /vms/{vm_id[:8]}/network-policy — updated")
    return {"ok": True}


@app.get("/vms/{vm_id}/network-policy")
def get_network_policy(vm_id: str):
    """Get the current network policy for a sandbox."""
    mgr = _require_manager()
    entry = _require_entry(vm_id)
    return {"network_policy": entry.network_policy}


# ── Guest agent proxy endpoints ───────────────────────────────────────────

@app.post("/vms/{vm_id}/exec")
async def exec_command(vm_id: str, request: Request):
    """Proxy POST /exec to the guest agent."""
    print(f"POST /vms/{vm_id[:8]}/exec")
    entry = _require_entry(vm_id)
    body = await request.body()
    status, resp_body = await _agent_request_async(entry, "POST", "/exec", body)
    return Response(content=resp_body, status_code=status, media_type="application/json")


@app.post("/vms/{vm_id}/processes")
async def create_process(vm_id: str, request: Request):
    """Proxy POST /processes to the guest agent."""
    print(f"POST /vms/{vm_id[:8]}/processes")
    entry = _require_entry(vm_id)
    body = await request.body()
    status, resp_body = await _agent_request_async(entry, "POST", "/processes", body)
    return Response(content=resp_body, status_code=status, media_type="application/json")


@app.get("/vms/{vm_id}/processes")
async def list_processes(vm_id: str):
    """Proxy GET /processes to the guest agent."""
    entry = _require_entry(vm_id)
    status, resp_body = await _agent_request_async(entry, "GET", "/processes")
    return Response(content=resp_body, status_code=status, media_type="application/json")


@app.post("/vms/{vm_id}/processes/{pid}/input")
async def process_input(vm_id: str, pid: int, request: Request):
    """Proxy stdin input to a guest process."""
    entry = _require_entry(vm_id)
    body = await request.body()
    status, resp_body = await _agent_request_async(entry, "POST", f"/processes/{pid}/input", body)
    return Response(content=resp_body, status_code=status, media_type="application/json")


@app.post("/vms/{vm_id}/processes/{pid}/signal")
async def process_signal(vm_id: str, pid: int, request: Request):
    """Proxy signal to a guest process."""
    entry = _require_entry(vm_id)
    body = await request.body()
    status, resp_body = await _agent_request_async(entry, "POST", f"/processes/{pid}/signal", body)
    return Response(content=resp_body, status_code=status, media_type="application/json")


@app.post("/vms/{vm_id}/processes/{pid}/resize")
async def process_resize(vm_id: str, pid: int, request: Request):
    """Proxy resize to a guest process."""
    entry = _require_entry(vm_id)
    body = await request.body()
    status, resp_body = await _agent_request_async(entry, "POST", f"/processes/{pid}/resize", body)
    return Response(content=resp_body, status_code=status, media_type="application/json")


# ── Filesystem proxy endpoints ───────────────────────────────────────────

@app.get("/vms/{vm_id}/files")
async def read_file(vm_id: str, path: str):
    """Proxy GET /files to the guest agent."""
    entry = _require_entry(vm_id)
    status, resp_body = await _agent_request_async(entry, "GET", f"/files?path={urllib.parse.quote(path, safe='/')}")
    media = "application/octet-stream" if status == 200 else "application/json"
    return Response(content=resp_body, status_code=status, media_type=media)


@app.post("/vms/{vm_id}/files")
async def write_file(vm_id: str, path: str, request: Request, mode: str = "0644"):
    """Proxy POST /files to the guest agent."""
    entry = _require_entry(vm_id)
    body = await request.body()
    status, resp_body = await _agent_request_async(entry, "POST", f"/files?path={urllib.parse.quote(path, safe='/')}&mode={mode}", body)
    return Response(content=resp_body, status_code=status, media_type="application/json")


@app.get("/vms/{vm_id}/files/stat")
async def stat_file(vm_id: str, path: str):
    entry = _require_entry(vm_id)
    status, resp_body = await _agent_request_async(entry, "GET", f"/files/stat?path={urllib.parse.quote(path, safe='/')}")
    return Response(content=resp_body, status_code=status, media_type="application/json")


@app.get("/vms/{vm_id}/files/list")
async def list_files(vm_id: str, path: str):
    entry = _require_entry(vm_id)
    status, resp_body = await _agent_request_async(entry, "GET", f"/files/list?path={urllib.parse.quote(path, safe='/')}")
    return Response(content=resp_body, status_code=status, media_type="application/json")


@app.post("/vms/{vm_id}/files/mkdir")
async def mkdir(vm_id: str, path: str, parents: bool = True):
    entry = _require_entry(vm_id)
    status, resp_body = await _agent_request_async(entry, "POST", f"/files/mkdir?path={urllib.parse.quote(path, safe='/')}&parents={'true' if parents else 'false'}")
    return Response(content=resp_body, status_code=status, media_type="application/json")


@app.delete("/vms/{vm_id}/files")
async def delete_file(vm_id: str, path: str, recursive: bool = False):
    entry = _require_entry(vm_id)
    status, resp_body = await _agent_request_async(entry, "DELETE", f"/files?path={urllib.parse.quote(path, safe='/')}&recursive={'true' if recursive else 'false'}")
    return Response(content=resp_body, status_code=status, media_type="application/json")


# ── Terminal WebSocket ────────────────────────────────────────────────────

@app.websocket("/vms/{vm_id}/terminal")
async def terminal_ws(websocket: WebSocket, vm_id: str):
    """Interactive terminal: creates a PTY process on the guest and bridges I/O."""
    print(f"WS /vms/{vm_id[:8]}/terminal — connecting")
    daemon = _get_daemon()
    if not daemon.manager:
        await websocket.close(code=1011, reason="Manager not initialized")
        return
    entry = daemon.manager.get_entry(vm_id)
    if not entry:
        print(f"WS /vms/{vm_id[:8]}/terminal — VM not found")
        await websocket.close(code=1011, reason="VM not found")
        return

    await websocket.accept()
    print(f"WS /vms/{vm_id[:8]}/terminal — accepted")

    # Start a PTY process on the guest agent
    try:
        create_body = json.dumps({
            "cmd": ["/bin/sh", "-i"],
            "pty": True,
            "cols": 120,
            "rows": 40,
        }).encode()
        status, resp_body = await _agent_request_async(entry, "POST", "/processes", create_body)
        if status != 201:
            print(f"WS /vms/{vm_id[:8]}/terminal — PTY create failed: {resp_body}")
            await websocket.close(code=1011, reason="Failed to create PTY process")
            return
        proc_info = json.loads(resp_body)
        guest_pid = proc_info["pid"]
    except Exception as e:
        log.exception("Failed to create guest PTY process")
        await websocket.close(code=1011, reason=str(e))
        return

    print(f"WS /vms/{vm_id[:8]}/terminal — guest PTY pid={guest_pid}")

    # Connect to guest WebSocket for output streaming
    import websockets.sync.client as ws_sync
    import base64

    loop = asyncio.get_event_loop()
    closed = False

    def _read_guest_ws():
        """Background thread: read from guest WS, forward to host WS."""
        nonlocal closed
        try:
            orig_fd = _enter_netns(entry.ns_name)
            try:
                guest_ws_url = f"ws://{entry.guest_ip}:5000/processes/{guest_pid}/output"
                guest_ws = ws_sync.connect(guest_ws_url)
            finally:
                _restore_netns(orig_fd)

            for message in guest_ws:
                if closed:
                    break
                try:
                    ev = json.loads(message)
                    if ev.get("type") in ("stdout", "stderr") and ev.get("data"):
                        raw = base64.b64decode(ev["data"])
                        asyncio.run_coroutine_threadsafe(
                            websocket.send_bytes(raw), loop
                        )
                    elif ev.get("type") == "exit":
                        break
                except Exception:
                    pass
            guest_ws.close()
        except Exception:
            pass

    reader_thread = threading.Thread(target=_read_guest_ws, daemon=True)
    reader_thread.start()

    try:
        while True:
            data = await websocket.receive_bytes()
            # Forward input to guest process
            try:
                await _agent_request_async(entry, "POST", f"/processes/{guest_pid}/input", data, timeout=5)
            except Exception:
                pass
    except WebSocketDisconnect:
        print(f"WS /vms/{vm_id[:8]}/terminal — client disconnected")
    except Exception:
        log.exception("WS /vms/%s/terminal — error", vm_id[:8])
    finally:
        closed = True
        # Kill the guest shell process
        try:
            await _agent_request_async(entry, "POST", f"/processes/{guest_pid}/signal",
                          json.dumps({"signal": 9}).encode(), timeout=2)
        except Exception:
            pass


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

    def _run_build():
        try:
            _build_template(name, dockerfile, rootfs_size_mb=rootfs_size_mb)
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
    # Remove snapshot files
    tdir = tmpl.get("template_dir", "")
    if tdir and os.path.isdir(tdir):
        shutil.rmtree(tdir, ignore_errors=True)
    _delete_template(template_id)
    print(f"DELETE /templates/{template_id} — deleted")
    return {"ok": True}


# ── FlintDaemon ──────────────────────────────────────────────────────────

class FlintDaemon:
    def __init__(self) -> None:
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
        self._setup_networking()
        self._create_golden_snapshot()
        self._register_templates()
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

    def _setup_networking(self) -> None:
        _ensure_bridge()
        print("Bridge ready.")

    def _create_golden_snapshot(self) -> None:
        print("Creating golden snapshot...")
        shutil.rmtree(GOLDEN_DIR, ignore_errors=True)
        create_golden_snapshot()
        self.golden_ready = True
        print("Golden snapshot ready.")

    def _register_templates(self) -> None:
        os.makedirs(TEMPLATES_DIR, exist_ok=True)
        register_template(DEFAULT_TEMPLATE_ID, "Default (Alpine)", GOLDEN_DIR)
        print("Default template registered.")

    def _start_pool(self) -> None:
        start_pool()
        print("Rootfs pool started.")

    def _init_state_store(self) -> None:
        from flint.core._state_store import StateStore
        self._state_store = StateStore(DAEMON_DB_PATH)
        print("State store initialized.")

    def _init_manager(self) -> None:
        self.manager = SandboxManager(state_store=self._state_store)
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
            stop_pool()
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
        uvicorn.run(app, host=DAEMON_HOST, port=DAEMON_PORT, log_level="warning")

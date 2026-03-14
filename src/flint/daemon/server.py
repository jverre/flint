from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import threading
import time

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
import uvicorn

from flint.core.config import (
    log, DAEMON_HOST, DAEMON_PORT, DAEMON_DIR, DAEMON_STATE_PATH, DAEMON_PID_PATH,
    GOLDEN_DIR, TEMPLATES_DIR, DEFAULT_TEMPLATE_ID,
)
from flint.core.manager import SandboxManager
from flint.core._snapshot import create_golden_snapshot, golden_snapshot_exists
from flint.core._pool import start_pool, stop_pool
from flint.core._netns import _ensure_bridge
from flint.core._template_registry import (
    list_templates, get_template, register_template, delete_template as _delete_template,
    get_template_dir,
)
from flint.core._template_build import build_template as _build_template

app = FastAPI()
manager: SandboxManager | None = None
_golden_ready = False


def _write_state() -> None:
    """Atomically write daemon state to JSON file."""
    state = {
        "pid": os.getpid(),
        "started_at": _started_at,
        "golden_snapshot_ready": _golden_ready,
        "vms": {},
    }
    if manager:
        for d in manager.list_dicts():
            state["vms"][d["vm_id"]] = d
    tmp_path = DAEMON_STATE_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(state, f)
    os.rename(tmp_path, DAEMON_STATE_PATH)


_started_at: float = 0.0


def _require_manager() -> SandboxManager:
    if not manager:
        raise HTTPException(status_code=503, detail="Manager not initialized")
    return manager


@app.get("/health")
def health():
    print("GET /health")
    return {"status": "ok", "golden_snapshot_ready": _golden_ready}


@app.post("/vms")
def create_vm(template_id: str = DEFAULT_TEMPLATE_ID, allow_internet_access: bool = True, use_pool: bool = True, use_pyroute2: bool = True):
    print(f"POST /vms — creating VM (template={template_id}, internet={allow_internet_access})...")
    mgr = _require_manager()
    if template_id == DEFAULT_TEMPLATE_ID and not _golden_ready:
        raise HTTPException(status_code=503, detail="Golden snapshot not ready")
    vm_id = mgr.create(template_id=template_id, allow_internet_access=allow_internet_access, use_pool=use_pool, use_pyroute2=use_pyroute2)
    result = mgr.get_dict(vm_id) or {"vm_id": vm_id}
    _write_state()
    print(f"POST /vms — created {vm_id[:8]}")
    return {"vm": result}


@app.get("/vms")
def list_vms():
    if not manager:
        return {"vms": []}
    entries = manager.list_dicts()
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
    mgr = _require_manager()
    if mgr.get_dict(vm_id) is None:
        print(f"DELETE /vms/{vm_id[:8]} — not found")
        raise HTTPException(status_code=404, detail="VM not found")
    mgr.kill(vm_id)
    _write_state()
    print(f"DELETE /vms/{vm_id[:8]} — killed")
    return {"ok": True}


@app.websocket("/vms/{vm_id}/terminal")
async def terminal_ws(websocket: WebSocket, vm_id: str):
    print(f"WS /vms/{vm_id[:8]}/terminal — connecting")
    if not manager:
        await websocket.close(code=1011, reason="Manager not initialized")
        return
    entry = manager.get_entry(vm_id)
    if not entry:
        print(f"WS /vms/{vm_id[:8]}/terminal — VM not found")
        await websocket.close(code=1011, reason="VM not found")
        return

    await websocket.accept()
    print(f"WS /vms/{vm_id[:8]}/terminal — accepted")

    loop = asyncio.get_event_loop()

    def on_output(data: bytes):
        try:
            asyncio.run_coroutine_threadsafe(
                websocket.send_bytes(data), loop
            )
        except Exception:
            pass

    entry.subscribe_output(on_output)
    try:
        while True:
            data = await websocket.receive_bytes()
            entry.send_raw(data)
    except WebSocketDisconnect:
        print(f"WS /vms/{vm_id[:8]}/terminal — client disconnected")
    except Exception:
        log.exception("WS /vms/%s/terminal — error", vm_id[:8])
    finally:
        entry.unsubscribe_output(on_output)


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


class FlintDaemon:
    def run(self) -> None:
        global manager, _golden_ready, _started_at

        _started_at = time.time()
        os.makedirs(DAEMON_DIR, exist_ok=True)

        # Write PID file
        with open(DAEMON_PID_PATH, "w") as f:
            f.write(str(os.getpid()))

        # Ensure bridge is ready for VM networking
        _ensure_bridge()
        print("Bridge ready.")

        # Create golden snapshot
        print("Creating golden snapshot...")
        shutil.rmtree(GOLDEN_DIR, ignore_errors=True)
        create_golden_snapshot()
        _golden_ready = True
        print("Golden snapshot ready.")

        # Register existing golden snapshot as "default" template
        os.makedirs(TEMPLATES_DIR, exist_ok=True)
        register_template(DEFAULT_TEMPLATE_ID, "Default (Alpine)", GOLDEN_DIR)
        print("Default template registered.")

        # Start rootfs pool
        start_pool()
        print("Rootfs pool started.")

        # Initialize manager
        manager = SandboxManager()
        _write_state()

        print(f"Daemon ready on {DAEMON_HOST}:{DAEMON_PORT}")

        def _shutdown(signum, frame):
            print("\nShutting down...")
            if manager:
                for vm_id in manager.vm_ids():
                    try:
                        manager.kill(vm_id)
                    except Exception:
                        pass
            stop_pool()
            # Clean up state files
            for path in (DAEMON_STATE_PATH, DAEMON_PID_PATH):
                try:
                    os.unlink(path)
                except OSError:
                    pass
            raise SystemExit(0)

        signal.signal(signal.SIGINT, _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)

        uvicorn.run(app, host=DAEMON_HOST, port=DAEMON_PORT, log_level="warning")

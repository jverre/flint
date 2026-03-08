from __future__ import annotations

import asyncio
import json
import os
import signal
import time

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
import uvicorn

from flint.core.config import (
    log, DAEMON_HOST, DAEMON_PORT, DAEMON_DIR, DAEMON_STATE_PATH, DAEMON_PID_PATH, GOLDEN_DIR,
)
from flint.core.manager import SandboxManager
from flint.core._snapshot import create_golden_snapshot, golden_snapshot_exists
from flint.core._pool import start_pool, stop_pool

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
def create_vm():
    print("POST /vms — creating VM...")
    mgr = _require_manager()
    if not _golden_ready:
        raise HTTPException(status_code=503, detail="Golden snapshot not ready")
    vm_id = mgr.create()
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


class FlintDaemon:
    def run(self) -> None:
        global manager, _golden_ready, _started_at

        _started_at = time.time()
        os.makedirs(DAEMON_DIR, exist_ok=True)

        # Write PID file
        with open(DAEMON_PID_PATH, "w") as f:
            f.write(str(os.getpid()))

        # Create golden snapshot
        print("Creating golden snapshot...")
        import shutil
        shutil.rmtree(GOLDEN_DIR, ignore_errors=True)
        create_golden_snapshot()
        _golden_ready = True
        print("Golden snapshot ready.")

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

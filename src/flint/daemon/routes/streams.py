"""Live streams: the daemon's events, and each VM's log lines."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from flint.core import _logs

from .. import events
from ..auth import principal_of

log = logging.getLogger("flint.daemon")
router = APIRouter()


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


@router.websocket("/events")
async def events_ws(websocket: WebSocket):
    """Stream daemon-wide lifecycle events (see events.py) to a subscriber, its tenants' only."""
    bus = events.get_bus()
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
        log.exception("WS /events failed")
    finally:
        bus.unsubscribe(q)


@router.websocket("/vms/{vm_id}/logs")
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
        log.exception("WS /vms/%s/logs failed", vm_id[:8])
    finally:
        bus.unsubscribe(vm_id, q)

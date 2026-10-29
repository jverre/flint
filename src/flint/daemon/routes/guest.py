"""Inside a VM: commands, processes, files and the terminal, relayed to its guest agent.

The API parses everything the guest sends (in the unprivileged process on
Linux); the services only hand over a connection to the VM's agent.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import time
import urllib.parse

from fastapi import APIRouter, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from flint.core.errors import NotFound
from flint.core.guest import GuestAgent, bridge_terminal
from flint.core.guest import http as guest_http

from .. import events
from . import ServicesDep, run, services_of

log = logging.getLogger("flint.daemon")
router = APIRouter()

# The API holds at most this much of a guest agent's reply and streams the
# rest through, so a guest that is slow or sends a lot costs a coroutine, a
# socket and a chunk, however many requests a tenant makes.
_HELD_REPLY = 1 << 20


async def _guest_reply(services, vm_id: str, method: str, path: str, body=None, timeout: float = 65):
    """Send a request to the VM's agent: ``(reply, its first bytes, whether that is all)``."""
    sock = await run(services.connect_agent, vm_id)
    reply = await guest_http.open(sock, method, path, body, timeout)
    head, complete = await reply.head(_HELD_REPLY)
    return reply, head, complete


def _relay(reply, head: bytes, complete: bool, media_type: str = "application/json", on_end=None) -> Response:
    """The guest's reply as the API's response, streaming what wasn't held.

    ``on_end`` gets the body's last few KiB once a streamed body has ended.
    """
    if complete:
        return Response(content=head, status_code=reply.status, media_type=media_type)

    async def body():
        tail = head[-4096:]
        try:
            yield head
            async for chunk in reply.rest():
                tail = (tail + chunk)[-4096:]
                yield chunk
        finally:
            reply.close()  # now, not when the generator is collected, if the client hangs up
        if on_end:
            on_end(tail)

    # A body cut short then shows as one, not as a complete shorter file.
    headers = {"content-length": reply.headers["content-length"]} if "content-length" in reply.headers else None
    return StreamingResponse(body(), status_code=reply.status, media_type=media_type, headers=headers)


async def _proxy(services, vm_id: str, method: str, path: str, body=None) -> Response:
    return _relay(*await _guest_reply(services, vm_id, method, path, body))


def _command_text(body: bytes) -> str:
    """The command an exec or process request runs, for its event (``sh -c`` scripts as is)."""
    try:
        cmd = json.loads(body).get("cmd") or []
    except (ValueError, AttributeError):
        return ""
    if not isinstance(cmd, list):
        return str(cmd)[:500]
    if len(cmd) >= 3 and cmd[1] == "-c" and str(cmd[0]).endswith("sh"):
        return str(cmd[2])[:500]
    return shlex.join(str(c) for c in cmd)[:500]


def _quote(path: str) -> str:
    return urllib.parse.quote(path, safe="/")


# ── Commands and processes ─────────────────────────────────────────────────

@router.post("/vms/{vm_id}/exec")
async def exec_command(vm_id: str, request: Request, services: ServicesDep):
    """Run a command in the guest, and record it as a ``vm.exec`` event."""
    body = await request.body()
    t0 = time.monotonic()
    reply, head, complete = await _guest_reply(services, vm_id, "POST", "/exec", body)
    event = {"vm_id": vm_id, "cmd": _command_text(body), "duration_ms": round((time.monotonic() - t0) * 1000, 1)}
    if not complete:
        # Too much output to hold: the exit code is the reply's last field.
        def on_end(tail: bytes) -> None:
            code = re.search(rb'"exit_code":\s*(-?\d+)\s*}\s*$', tail)
            events.publish("vm.exec", **event, exit_code=int(code[1]) if code else None)
        return _relay(reply, head, complete, on_end=on_end)
    try:
        result = json.loads(head) if reply.status == 200 else {}
        event.update(exit_code=result.get("exit_code"), stdout_tail=str(result.get("stdout", ""))[-256:],
                     stderr_tail=str(result.get("stderr", ""))[-256:])
    except ValueError:
        pass
    if reply.status != 200:
        event["error"] = head[:256].decode(errors="replace")
    events.publish("vm.exec", **event)
    return _relay(reply, head, complete)


@router.post("/vms/{vm_id}/processes")
async def create_process(vm_id: str, request: Request, services: ServicesDep):
    """Start a process in the guest, and record it as a ``vm.process_started`` event."""
    body = await request.body()
    reply, head, complete = await _guest_reply(services, vm_id, "POST", "/processes", body)
    if 200 <= reply.status < 300:  # the guest agent answers 201 Created
        try:
            pid = json.loads(head).get("pid")
        except (ValueError, AttributeError):
            pid = None
        events.publish("vm.process_started", vm_id=vm_id, cmd=_command_text(body), pid=pid)
    return _relay(reply, head, complete)


@router.get("/vms/{vm_id}/processes")
async def list_processes(vm_id: str, services: ServicesDep):
    return await _proxy(services, vm_id, "GET", "/processes")


@router.post("/vms/{vm_id}/processes/{pid}/input")
async def process_input(vm_id: str, pid: int, request: Request, services: ServicesDep):
    return await _proxy(services, vm_id, "POST", f"/processes/{pid}/input", await request.body())


@router.post("/vms/{vm_id}/processes/{pid}/signal")
async def process_signal(vm_id: str, pid: int, request: Request, services: ServicesDep):
    return await _proxy(services, vm_id, "POST", f"/processes/{pid}/signal", await request.body())


@router.post("/vms/{vm_id}/processes/{pid}/resize")
async def process_resize(vm_id: str, pid: int, request: Request, services: ServicesDep):
    return await _proxy(services, vm_id, "POST", f"/processes/{pid}/resize", await request.body())


# ── Files ──────────────────────────────────────────────────────────────────

@router.get("/vms/{vm_id}/files")
async def read_file(vm_id: str, path: str, services: ServicesDep):
    reply, head, complete = await _guest_reply(services, vm_id, "GET", f"/files?path={_quote(path)}")
    media = "application/octet-stream" if reply.status == 200 else "application/json"
    return _relay(reply, head, complete, media)


@router.post("/vms/{vm_id}/files")
async def write_file(vm_id: str, path: str, request: Request, services: ServicesDep, mode: str = "0644"):
    return await _proxy(services, vm_id, "POST", f"/files?path={_quote(path)}&mode={urllib.parse.quote(mode)}",
                        request.stream())


@router.get("/vms/{vm_id}/files/stat")
async def stat_file(vm_id: str, path: str, services: ServicesDep):
    return await _proxy(services, vm_id, "GET", f"/files/stat?path={_quote(path)}")


@router.get("/vms/{vm_id}/files/list")
async def list_files(vm_id: str, path: str, services: ServicesDep):
    return await _proxy(services, vm_id, "GET", f"/files/list?path={_quote(path)}")


@router.post("/vms/{vm_id}/files/mkdir")
async def mkdir(vm_id: str, path: str, services: ServicesDep, parents: bool = True):
    return await _proxy(services, vm_id, "POST", f"/files/mkdir?path={_quote(path)}&parents={'true' if parents else 'false'}")


@router.delete("/vms/{vm_id}/files")
async def delete_file(vm_id: str, path: str, services: ServicesDep, recursive: bool = False):
    return await _proxy(services, vm_id, "DELETE", f"/files?path={_quote(path)}&recursive={'true' if recursive else 'false'}")


# ── Terminal ───────────────────────────────────────────────────────────────

@router.websocket("/vms/{vm_id}/terminal")
async def terminal_ws(websocket: WebSocket, vm_id: str):
    """An interactive shell on a PTY in the guest."""
    services = services_of(websocket)
    if await run(services.get_vm, vm_id) is None:
        await websocket.close(code=1011, reason="VM not found")
        return

    def connect():
        return services.connect_agent(vm_id)

    try:
        await bridge_terminal(GuestAgent(connect), websocket)
    except NotFound:
        log.info("terminal of VM %s: not running", vm_id[:8])
        await websocket.close(code=1011, reason="VM not found")
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("terminal of VM %s failed", vm_id[:8])

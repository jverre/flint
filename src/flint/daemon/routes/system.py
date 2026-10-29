"""The daemon itself: liveness, its settings, and the web UI."""

from __future__ import annotations

import os

from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from ..models import ConfigPatch
from . import ServicesDep, run

router = APIRouter()


@router.get("/health")
async def health(services: ServicesDep):
    return await run(services.health)


@router.get("/config")
async def get_config(services: ServicesDep):
    return await run(services.get_config)


@router.patch("/config")
async def patch_config(body: ConfigPatch, services: ServicesDep):
    return await run(services.patch_config, body.model_dump(exclude_unset=True))


# ── Web UI ─────────────────────────────────────────────────────────────────
# The admin UI (web/) is built into src/flint/daemon/ui and served from the same
# origin as the API, so it needs no CORS. FLINT_UI_DIR overrides the location.

_UI_DIR = os.path.realpath(os.environ.get("FLINT_UI_DIR") or os.path.join(os.path.dirname(__file__), "..", "ui"))
_UI_MISSING = """<!doctype html><meta charset="utf-8"><title>Flint</title>
<body style="font:14px system-ui;background:#151922;color:#eef1f7;display:grid;place-items:center;height:100vh;margin:0">
<div><h1 style="font-size:18px">The Flint web UI isn't built</h1>
<p>Build it with <code>scripts/build-ui.sh</code> (needs Node 20+), then reload.</p></div>"""


@router.get("/", include_in_schema=False)
def root_redirect():
    return RedirectResponse("/ui/")


@router.get("/ui", include_in_schema=False)
@router.get("/ui/{path:path}", include_in_schema=False)
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

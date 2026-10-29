"""The daemon's HTTP API: the FastAPI app, its authentication and how errors become responses.

The endpoints are in ``routes/``; they call the :class:`~flint.daemon.services.Services`
in ``app.state.services``, which the process serving the app installs
(``lifecycle.FlintDaemon`` or ``frontend.main``).
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from flint.core import _logs
from flint.core.errors import FlintError
from flint.core.guest.http import GuestAgentError

from . import events, sso
from .auth import AuthError, AuthMiddleware
from .routes import routers

log = logging.getLogger("flint.daemon")

app = FastAPI()
app.include_router(sso.router)
for _router in routers():
    app.include_router(_router)


def _services():
    return getattr(app.state, "services", None)


def _vm_tenant(vm_id: str) -> str | None:
    services = _services()
    vm = services.get_vm(vm_id) if services else None
    return vm.get("tenant", "") if vm else None


app.add_middleware(
    AuthMiddleware,
    tokens=lambda: getattr(_services(), "tokens", None),
    cluster_mode=lambda: bool(getattr(_services(), "cluster_mode", False)),
    vm_tenant=_vm_tenant,
    eventlog=lambda: getattr(_services(), "eventlog", None),
)


def _error(status: int | None, detail: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status or 500, headers=headers)


@app.exception_handler(FlintError)
async def _flint_error(_request: Request, e: FlintError):
    """The one place the services' errors become HTTP statuses (404 not found, 409 wrong state…)."""
    return _error(e.status, e.detail, e.headers)


@app.exception_handler(AuthError)
async def _auth_error(_request: Request, e: AuthError):
    return _error(e.status, e.detail)


@app.exception_handler(GuestAgentError)
async def _guest_agent_error(_request: Request, e: GuestAgentError):
    return _error(e.status, e.detail)


@app.exception_handler(RequestValidationError)
async def _invalid_request(_request: Request, e: RequestValidationError):
    """A request that doesn't match its model: 400, saying which field is wrong and why."""
    problems = []
    for err in e.errors():
        where = ".".join(str(part) for part in err.get("loc", ()) if part not in ("body", "query", "path"))
        msg = str(err.get("msg", "")).removeprefix("Value error, ")
        problems.append(f"{where}: {msg}" if where else msg)
    return _error(400, "; ".join(problems) or "Invalid request")


@app.on_event("startup")
async def _start_buses():
    loop = asyncio.get_running_loop()
    events.init_bus(loop)
    _logs.init_bus(loop)
    services = _services()
    if services is not None:
        services.start(loop)


def enable_sso() -> None:
    """Sign-in with identity providers, if ``sso.json`` configures any. Call before serving."""
    from flint.core import config
    if config.CLUSTER_MODE:
        return  # the control plane signs people in
    try:
        settings = sso.load()
    except ValueError as e:
        raise SystemExit(f"SSO config: {e}")
    if settings is None:
        return
    from starlette.middleware.sessions import SessionMiddleware
    from flint.core._secrets import derived_key
    app.add_middleware(SessionMiddleware, secret_key=derived_key("sso-login"), session_cookie=sso.LOGIN_COOKIE,
                       max_age=600, same_site="lax", https_only=settings.secure_cookies)
    app.state.sso = settings
    for p in settings.config.providers.values():
        log.info("SSO: %s (%s); its redirect URI is %s", p.label, p.id, settings.redirect_uri(p.id))

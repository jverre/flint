"""The API's endpoints, one router per resource.

Endpoints parse the request, decide what the caller may see or act for
(tenancy is an API concern: the services trust the tenant they're given),
and call the :class:`~flint.daemon.services.Services` in ``app.state``.
Failures are :mod:`flint.core.errors`, which ``server.py`` maps to responses.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import Depends, Request
from starlette.requests import HTTPConnection

from flint.core.config import DEFAULT_TEMPLATE_ID
from flint.core.errors import Unavailable

from ..auth import Principal, principal_of
from ..services import Services


def services_of(conn: HTTPConnection) -> Services:
    services = getattr(conn.app.state, "services", None)
    if services is None:
        raise Unavailable("Daemon not initialized")
    return services


def _principal(request: Request) -> Principal:
    return principal_of(request)


ServicesDep = Annotated[Services, Depends(services_of)]
PrincipalDep = Annotated[Principal, Depends(_principal)]


async def run(fn, /, *args, **kwargs):
    """Call a (blocking) service in a thread, keeping the request's context (who the actor is)."""
    return await asyncio.to_thread(fn, *args, **kwargs)


def tenant_scope(principal: Principal) -> list[str] | None:
    """The tenants a caller's lookups are limited to (None: every tenant)."""
    return None if principal.is_admin or not principal.tenants else list(principal.tenants)


def visible_template(services: Services, principal: Principal, template_id: str) -> dict | None:
    """A template the caller can see: shared (no tenant) or one of their tenants'."""
    template = services.get_template(template_id)
    if template is None and template_id == DEFAULT_TEMPLATE_ID:
        return {"template_id": template_id}
    if template is None or (template.get("tenant") and not principal.can_tenant(template["tenant"])):
        return None
    return template


def routers():
    from . import admin, guest, streams, system, templates, vms, volumes
    return [system.router, vms.router, guest.router, streams.router, templates.router, volumes.router, admin.router]

"""Tenancy and accountability: API tokens, tenant quotas and networks, stored events and the audit log.

The same API as a cluster control plane's (the quotas have more limits).
"""

from __future__ import annotations

from fastapi import APIRouter

from flint.core.errors import Forbidden, NotFound, Unavailable

from ..models import CreateToken, Quota, TenantNetwork
from . import PrincipalDep, ServicesDep, run, tenant_scope

router = APIRouter()


def _eventlog(services):
    if services.eventlog is None:
        raise Unavailable("Event log not initialized")
    return services.eventlog


def _tokens(services):
    if services.tokens is None:
        raise Unavailable("Tokens not initialized")
    return services.tokens


# ── Stored events and the audit log ───────────────────────────────────────

@router.get("/v1/events")
async def list_events(principal: PrincipalDep, services: ServicesDep, since: int = 0, vm_id: str = "",
                      tenant: str = "", type: str = "", actor: str = "", limit: int = 500):
    """Stored events after ``since`` (a ``seq``), oldest first, of the caller's
    tenants. ``next`` is the ``since`` for the following page."""
    events = await run(_eventlog(services).query, since=since, tenants=tenant_scope(principal), tenant=tenant,
                       vm_id=vm_id, type=type, actor=actor, limit=limit)
    return {"events": events, "next": events[-1]["seq"] if events else since}


@router.get("/v1/audit")
async def audit_log(services: ServicesDep, since: int = 0, limit: int = 500):
    """The audit log (admin only): every API call that changed something, hash-chained."""
    entries = await run(_eventlog(services).audit_entries, since, limit)
    return {"entries": entries, "next": entries[-1]["seq"] if entries else since}


@router.get("/v1/audit/verify")
async def audit_verify(services: ServicesDep):
    """Recompute the audit chain; ``ok`` is false, with where, if an entry was changed or removed."""
    return await run(_eventlog(services).verify_audit)


# ── API tokens (admin only) ───────────────────────────────────────────────

@router.post("/v1/tokens")
async def create_token(body: CreateToken, services: ServicesDep):
    """Create an ``admin`` or ``service`` token (``tenants`` pins a service
    token to those tenants). The token is in the answer, and only there."""
    record, token = await run(_tokens(services).create, body.kind, body.name, body.tenants)
    return {**record, "token": token}


@router.get("/v1/tokens")
async def list_tokens(services: ServicesDep):
    return {"tokens": await run(_tokens(services).list)}


@router.delete("/v1/tokens/{token_id}")
async def revoke_token(token_id: str, services: ServicesDep):
    if not await run(_tokens(services).revoke, token_id):
        raise NotFound("Token not found")
    return {"ok": True}


# ── Tenants ───────────────────────────────────────────────────────────────

@router.get("/v1/tenants/{tenant}/network")
async def get_tenant_network(tenant: str, principal: PrincipalDep, services: ServicesDep):
    if not principal.can_tenant(tenant):
        raise Forbidden("Not allowed for this tenant")
    return {"tenant": tenant, "allow_same_tenant": await run(services.get_tenant_network, tenant)}


@router.put("/v1/tenants/{tenant}/network")
async def put_tenant_network(tenant: str, body: TenantNetwork, principal: PrincipalDep, services: ServicesDep):
    """Let the tenant's VMs reach each other, or not."""
    principal.resolve_tenant(tenant)
    await run(services.set_tenant_network, tenant, body.allow_same_tenant)
    return {"tenant": tenant, "allow_same_tenant": await run(services.get_tenant_network, tenant)}


@router.put("/v1/tenants/{tenant}/quota")
async def put_quota(tenant: str, body: Quota, principal: PrincipalDep, services: ServicesDep):
    """Set some of a tenant's limits (admin only): max_vms, max_vcpus,
    max_memory_mib, max_volume_gib. 0 = no limit; the others stay."""
    if not principal.is_admin:
        raise Forbidden("Admin token required")
    return await run(services.set_quota, tenant, body.model_dump(exclude_unset=True))


@router.get("/v1/tenants/{tenant}/quota")
async def get_quota(tenant: str, principal: PrincipalDep, services: ServicesDep):
    """A tenant's limits and what it uses now."""
    if not principal.can_tenant(tenant):
        raise Forbidden("Not allowed for this tenant")
    return await run(services.get_quota, tenant)

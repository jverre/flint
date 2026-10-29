"""Volumes: storage a tenant's sandboxes mount while running (mounts are under /vms)."""

from __future__ import annotations

from fastapi import APIRouter

from ..models import CopyVolume, CreateVolume
from . import PrincipalDep, ServicesDep, run, tenant_scope

router = APIRouter()


@router.get("/volumes")
async def list_volumes(principal: PrincipalDep, services: ServicesDep, tenant: str = ""):
    volumes = await run(services.list_volumes, tenant_scope(principal))
    return {"volumes": [v for v in volumes if not tenant or v.get("tenant") == tenant]}


@router.post("/volumes")
async def create_volume(body: CreateVolume, principal: PrincipalDep, services: ServicesDep):
    tenant = principal.resolve_tenant(body.tenant)
    return {"volume": await run(services.create_volume, body.name, body.size_gib, tenant)}


@router.post("/volumes/{volume}/snapshot")
async def snapshot_volume(volume: str, body: CopyVolume, principal: PrincipalDep, services: ServicesDep):
    """A read-only copy of the volume as it is now."""
    return {"volume": await run(services.snapshot_volume, volume, body.name, tenant_scope(principal))}


@router.post("/volumes/{volume}/fork")
async def fork_volume(volume: str, body: CopyVolume, principal: PrincipalDep, services: ServicesDep):
    """A writable copy of the volume as it is now."""
    return {"volume": await run(services.fork_volume, volume, body.name, tenant_scope(principal))}


@router.delete("/volumes/{volume}")
async def delete_volume(volume: str, principal: PrincipalDep, services: ServicesDep):
    await run(services.delete_volume, volume, tenant_scope(principal))
    return {"ok": True}

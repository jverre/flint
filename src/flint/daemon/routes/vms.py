"""VMs: their lifecycle, settings, network policy, mounts and history."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Request

from flint.core.config import DEFAULT_TEMPLATE_ID
from flint.core.errors import NotFound, SandboxNotFound, Unavailable
from flint.core.policy import as_json

from ..auth import principal_of
from ..models import CheckpointVm, CreateVm, Mount, PatchVm, PauseVm, Policy
from . import PrincipalDep, ServicesDep, run, tenant_scope, visible_template

router = APIRouter()


@router.post("/vms")
async def create_vm(request: Request, services: ServicesDep, body: CreateVm | None = None,
                    template_id: str = DEFAULT_TEMPLATE_ID, allow_internet_access: bool = True, use_pool: bool = True,
                    tenant: str = ""):
    """Create a VM. A cluster's control plane also passes, in the body, the VM's
    ``vm_id``, ``tenant``, ``availability``, and the snapshots to start it
    from (see :meth:`LocalServices.create_vm`)."""
    body = body or CreateVm()
    tenant = body.tenant or tenant
    if not services.cluster_mode:  # a cluster's control plane resolved these already
        principal = principal_of(request)
        tenant = principal.resolve_tenant(tenant)
        template = await run(visible_template, services, principal, template_id)
        if template is None or (template.get("tenant") and template["tenant"] != tenant):
            raise NotFound(f"Template '{template_id}' not found")
    vm = await run(
        services.create_vm, template_id=template_id, tenant=tenant, allow_internet_access=allow_internet_access,
        use_pool=use_pool, network_policy=as_json(body.network_policy), timeout_seconds=body.timeout_seconds,
        timeout_policy=body.timeout_policy, vm_id=body.vm_id or None, availability=body.availability,
        snapshot=body.snapshot, template_snapshot=body.template_snapshot, replace_shared_disk=body.replace_shared_disk,
    )
    return {"vm": vm}


@router.get("/vms")
async def list_vms(principal: PrincipalDep, services: ServicesDep, tenant: str = ""):
    vms = await run(services.list_vms)
    return {"vms": [vm for vm in vms if principal.can_tenant(vm.get("tenant", "")) and (not tenant or vm.get("tenant") == tenant)]}


@router.get("/vms/{vm_id}")
async def get_vm(vm_id: str, services: ServicesDep):
    vm = await run(services.get_vm, vm_id)
    if vm is None:
        raise SandboxNotFound("VM not found")
    return {"vm": vm}


@router.delete("/vms/{vm_id}")
async def delete_vm(vm_id: str, services: ServicesDep):
    await run(services.kill_vm, vm_id)
    return {"ok": True}


@router.patch("/vms/{vm_id}")
async def patch_vm(vm_id: str, body: PatchVm, services: ServicesDep):
    if body.timeout_seconds is not None:
        await run(services.set_timeout, vm_id, body.timeout_seconds, body.timeout_policy)
    elif await run(services.get_vm, vm_id) is None:
        raise SandboxNotFound("VM not found")
    return {"ok": True}


@router.post("/vms/{vm_id}/pause")
async def pause_vm(vm_id: str, services: ServicesDep, body: PauseVm | None = None):
    body = body or PauseVm()
    return await run(services.pause_vm, vm_id, body.snapshot_id or None, body.upload)


@router.post("/vms/{vm_id}/resume")
async def resume_vm(vm_id: str, services: ServicesDep):
    return {"vm": await run(services.resume_vm, vm_id)}


@router.post("/vms/{vm_id}/checkpoint")
async def checkpoint_vm(vm_id: str, body: CheckpointVm, services: ServicesDep):
    return await run(services.checkpoint_vm, vm_id, body.snapshot_id, body.kind, body.parent, body.upload)


@router.post("/vms/{vm_id}/live-migration/{action}")
async def live_migration(vm_id: str, action: str, services: ServicesDep,
                         body: Annotated[dict[str, Any], Body()] = None):
    # The body is the control plane's migration step (cluster/), passed through as is.
    return await run(services.live_migration, vm_id, action, body or {})


@router.get("/vms/{vm_id}/metrics")
async def get_vm_metrics(vm_id: str, services: ServicesDep, window: int = 60):
    return {"samples": await run(services.vm_metrics, vm_id, window)}


@router.put("/vms/{vm_id}/network-policy")
async def update_network_policy(vm_id: str, policy: Annotated[Policy, Body()], services: ServicesDep):
    """Replace the sandbox's network policy (credential injection rules)."""
    await run(services.set_network_policy, vm_id, as_json(policy))
    return {"ok": True}


@router.get("/vms/{vm_id}/network-policy")
async def get_network_policy(vm_id: str, services: ServicesDep):
    """The sandbox's network policy. Credential values are write-only: redacted here."""
    return {"network_policy": await run(services.get_network_policy, vm_id)}


# ── Mounts ─────────────────────────────────────────────────────────────────

@router.get("/vms/{vm_id}/mounts")
async def list_mounts(vm_id: str, services: ServicesDep):
    return {"mounts": await run(services.list_mounts, vm_id)}


@router.post("/vms/{vm_id}/mounts")
async def mount_volume(vm_id: str, body: Mount, services: ServicesDep):
    """Mount a volume (or a subpath of it) into a running sandbox."""
    mount = await run(services.mount_volume, vm_id, body.volume, body.path, body.subpath, body.readonly, body.exclusive)
    return {"mount": mount}


@router.delete("/vms/{vm_id}/mounts")
async def unmount_volume(vm_id: str, path: str, services: ServicesDep):
    await run(services.unmount_volume, vm_id, path)
    return {"ok": True}


# ── History ────────────────────────────────────────────────────────────────

@router.get("/vms/{vm_id}/timeline")
async def vm_timeline(vm_id: str, principal: PrincipalDep, services: ServicesDep, since: int = 0, limit: int = 1000):
    """Everything that happened to a VM: lifecycle, execs, mounts, with who did it."""
    if await run(services.get_vm, vm_id) is None:
        raise SandboxNotFound("VM not found")
    if services.eventlog is None:
        raise Unavailable("Event log not initialized")
    events = await run(services.eventlog.query, since=since, tenants=tenant_scope(principal), vm_id=vm_id, limit=limit)
    return {"events": events, "next": events[-1]["seq"] if events else since}

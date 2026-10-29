"""Templates: build them from a Dockerfile, list, inspect and delete them.

A template belongs to a tenant, or is shared (no tenant) if built before
tenancy; callers see shared templates and their tenants' own.
"""

from __future__ import annotations

from fastapi import APIRouter

from flint.core.errors import Conflict, Forbidden, NotFound

from ..models import BuildTemplate
from . import PrincipalDep, ServicesDep, run, visible_template

router = APIRouter()


@router.post("/templates/build")
async def build_template(body: BuildTemplate, principal: PrincipalDep, services: ServicesDep):
    """Build a template. With ``tenant`` (or a token pinned to one tenant) it
    belongs to that tenant; otherwise it belongs to the caller's default tenant."""
    tenant = ""
    if not services.cluster_mode:  # a cluster's control plane decided the owner
        from flint.core._template_registry import _slugify
        template_id = _slugify(body.name)
        tenant = principal.resolve_tenant(body.tenant)
        existing = await run(services.get_template, template_id)
        if existing is not None and not existing.get("tenant") and principal.is_admin and not body.tenant:
            tenant = ""  # an admin rebuilding a template shared before tenancy keeps it shared
        if existing is not None and existing.get("tenant", "") != tenant:
            if await run(visible_template, services, principal, template_id) is None:
                raise Forbidden(f"Template '{template_id}' already exists for another tenant")
            raise Conflict(f"Template '{template_id}' already exists")
    return await run(services.build_template, body.name, body.dockerfile, body.rootfs_size_mb, tenant)


@router.get("/templates")
async def list_templates(principal: PrincipalDep, services: ServicesDep):
    templates = await run(services.list_templates)
    return {"templates": [t for t in templates if not t.get("tenant") or principal.can_tenant(t["tenant"])]}


@router.get("/templates/{template_id}")
async def get_template(template_id: str, principal: PrincipalDep, services: ServicesDep):
    template = await run(visible_template, services, principal, template_id)
    if template is None:
        raise NotFound("Template not found")
    return {"template": template}


@router.delete("/templates/{template_id}")
async def delete_template(template_id: str, principal: PrincipalDep, services: ServicesDep):
    template = await run(visible_template, services, principal, template_id)
    if template is None:
        raise NotFound("Template not found")
    if not template.get("tenant") and principal.tenants and not principal.is_admin:
        raise Forbidden("Shared templates can only be deleted by an admin")
    await run(services.delete_template, template_id)
    return {"ok": True}

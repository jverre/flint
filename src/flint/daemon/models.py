"""Request bodies the API accepts, validated where they enter.

A request that doesn't match answers 400 with what's wrong (see
``server.py``). Fields the control plane may send that a body doesn't use
are ignored; bodies that set limits or settings refuse unknown fields.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, StrictBool, StrictInt

from flint.core.policy import NetworkPolicy


def _policy(value: Any) -> NetworkPolicy | None:
    return NetworkPolicy.optional(value)  # PolicyError is a ValueError: a validation error


# A network policy's JSON, parsed by core/policy.py.
Policy = Annotated[Any, AfterValidator(_policy)]
# What happens when a sandbox's timeout passes ("" from the control plane means the default).
TimeoutPolicy = Annotated[Literal["kill", "pause"], BeforeValidator(lambda v: v or "kill")]
Seconds = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Upload = Annotated[str, BeforeValidator(lambda v: v or "sync")]


class CreateVm(BaseModel):
    """``POST /vms``'s optional body (the template, internet access and tenant are query parameters).

    ``vm_id``, ``availability``, ``snapshot``, ``template_snapshot`` and
    ``replace_shared_disk`` come from a cluster's control plane only.
    """

    network_policy: Policy = None
    timeout_seconds: Seconds | None = None
    timeout_policy: TimeoutPolicy = "kill"
    tenant: str | None = None
    vm_id: str | None = None
    availability: str = ""
    snapshot: dict[str, Any] | None = None
    template_snapshot: dict[str, Any] | None = None
    replace_shared_disk: StrictBool = False


class PauseVm(BaseModel):
    """In a cluster, ``snapshot_id`` also exports the paused state to the snapshot store."""

    snapshot_id: str | None = None
    upload: Upload = "sync"


class CheckpointVm(BaseModel):
    snapshot_id: Annotated[str, Field(min_length=1)]
    kind: str = "checkpoint"
    parent: str = ""
    upload: Upload = "sync"


class PatchVm(BaseModel):
    timeout_seconds: Seconds | None = None
    timeout_policy: TimeoutPolicy = "kill"


class BuildTemplate(BaseModel):
    name: Annotated[str, Field(min_length=1)]
    dockerfile: Annotated[str, Field(min_length=1)]
    rootfs_size_mb: Annotated[StrictInt, Field(ge=64, le=16384)] = 500
    tenant: str | None = None


class CreateVolume(BaseModel):
    name: Any = None  # checked by the volume store, which says what a name may be
    size_gib: StrictInt | None = None
    tenant: str | None = None


class CopyVolume(BaseModel):
    name: Any = None


class Mount(BaseModel):
    volume: str
    path: str
    subpath: str = ""
    readonly: StrictBool = False
    exclusive: StrictBool = False


class CreateToken(BaseModel):
    """An ``admin`` or ``service`` token; ``tenants`` pins a service token to those tenants."""

    kind: Annotated[str, BeforeValidator(lambda v: v or "service")] = "service"
    name: Annotated[str, BeforeValidator(lambda v: v or "")] = ""
    tenants: Annotated[list[str], BeforeValidator(lambda v: v or [])] = []


class TenantNetwork(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allow_same_tenant: StrictBool


Limit = Annotated[StrictInt, Field(ge=0)]


class Quota(BaseModel):
    """Some of a tenant's limits (0 = no limit); the others stay."""

    model_config = ConfigDict(extra="forbid")

    max_vms: Limit | None = None
    max_vcpus: Limit | None = None
    max_memory_mib: Limit | None = None
    max_volume_gib: Limit | None = None


class ConfigPatch(BaseModel):
    """Settings that take effect at the next start (``config.CONFIG_FIELDS``)."""

    model_config = ConfigDict(extra="forbid")

    pool_target_size: int | None = None
    default_sandbox_timeout: int | None = None
    health_check_interval: float | None = None
    error_cleanup_delay: int | None = None

"""What the daemon's API does, independent of HTTP.

The routes (``daemon/routes``) parse requests, check who's asking and call a
:class:`Services`. There are two implementations of it:

* :class:`LocalServices`, in the process that owns the VMs: the daemon on
  macOS, a cluster host agent, and the Linux privileged runtime.
* ``frontend.RemoteServices``, in the Linux daemon's unprivileged API
  process: it sends each call over the runtime's socket to the
  ``LocalServices`` there (``daemon/runtime.py``).

Every operation takes and returns JSON values, so it can cross that socket,
and reports failures with :mod:`flint.core.errors`, which carry their HTTP
status. The methods marked :func:`operation` are the ones the runtime serves.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import threading
from typing import TYPE_CHECKING, Any, Protocol

from flint.core import _logs
from flint.core.config import DEFAULT_TEMPLATE_ID, WORKSPACE_DIR, log
from flint.core.errors import (
    Conflict, FlintError, Forbidden, HostFull, InvalidRequest, NotFound, SandboxNotFound, Unavailable, Unsupported,
)
from flint.core.policy import NetworkPolicy, PolicyError
from flint.core.types import Maintenance, SandboxState

from . import events

if TYPE_CHECKING:
    from flint.core._quotas import Usage

    from .lifecycle import FlintDaemon


def operation(method):
    """Mark a method the privileged runtime serves to the unprivileged API."""
    method.runtime_operation = True
    return method


def operations(cls) -> list[str]:
    """The names of ``cls``'s :func:`operation` methods."""
    return [name for name in dir(cls) if getattr(getattr(cls, name), "runtime_operation", False)]


class EventStore(Protocol):
    """The stored events and the audit log (``eventlog.EventLog``)."""

    def record(self, event: dict) -> int: ...
    def query(self, **filters: Any) -> list[dict]: ...
    def audit(self, **entry: Any) -> None: ...
    def audit_entries(self, since: int = 0, limit: int = 500) -> list[dict]: ...
    def verify_audit(self) -> dict: ...


class Services(Protocol):
    """What the routes need from the daemon, in either process layout."""

    tokens: Any  # auth.TokenStore | None
    eventlog: EventStore | None
    cluster_mode: bool  # a cluster host agent: the control plane is the only caller

    def start(self, loop) -> None: ...

    def health(self) -> dict: ...
    def get_config(self) -> dict: ...
    def patch_config(self, changes: dict) -> dict: ...

    def list_vms(self) -> list[dict]: ...
    def get_vm(self, vm_id: str) -> dict | None: ...
    def create_vm(self, **spec: Any) -> dict: ...
    def kill_vm(self, vm_id: str, missing_ok: bool = False) -> None: ...
    def pause_vm(self, vm_id: str, snapshot_id: str | None = None, upload: str = "sync") -> dict: ...
    def resume_vm(self, vm_id: str) -> dict: ...
    def checkpoint_vm(self, vm_id: str, snapshot_id: str, kind: str = "checkpoint", parent: str = "",
                      upload: str = "sync") -> dict: ...
    def live_migration(self, vm_id: str, action: str, body: dict) -> dict: ...
    def set_timeout(self, vm_id: str, timeout_seconds: float, policy: str = "kill") -> None: ...
    def get_network_policy(self, vm_id: str) -> dict | None: ...
    def set_network_policy(self, vm_id: str, policy: Any) -> None: ...
    def vm_metrics(self, vm_id: str, window: int = 60) -> list[dict]: ...
    def connect_agent(self, vm_id: str): ...

    def list_mounts(self, vm_id: str) -> list[dict]: ...
    def mount_volume(self, vm_id: str, volume: str, path: str, subpath: str = "", readonly: bool = False,
                     exclusive: bool = False) -> dict: ...
    def unmount_volume(self, vm_id: str, path: str) -> None: ...

    def list_volumes(self, tenants: list[str] | None = None) -> list[dict]: ...
    def create_volume(self, name: str, size_gib: int | None = None, tenant: str = "default") -> dict: ...
    def snapshot_volume(self, ref: str, name: str | None = None, tenants: list[str] | None = None) -> dict: ...
    def fork_volume(self, ref: str, name: str | None = None, tenants: list[str] | None = None) -> dict: ...
    def delete_volume(self, ref: str, tenants: list[str] | None = None) -> None: ...

    def list_templates(self) -> list[dict]: ...
    def get_template(self, template_id: str) -> dict | None: ...
    def build_template(self, name: str, dockerfile: str, rootfs_size_mb: int = 500, tenant: str = "") -> dict: ...
    def delete_template(self, template_id: str) -> None: ...

    def get_tenant_network(self, tenant: str) -> bool: ...
    def set_tenant_network(self, tenant: str, enabled: bool) -> None: ...
    def get_quota(self, tenant: str) -> dict: ...
    def set_quota(self, tenant: str, changes: dict) -> dict: ...


class _Unfenced(FlintError):
    """A leased VM started but couldn't be fenced, so it was killed. 503: the scheduler places it elsewhere."""

    status = 503


def _flag(name: str, value: object) -> bool:
    if type(value) is not bool:
        raise InvalidRequest(f"'{name}' must be a boolean")
    return value


def _policy(value: object) -> NetworkPolicy | None:
    try:
        return NetworkPolicy.optional(value)
    except PolicyError as e:
        raise InvalidRequest(str(e)) from e


def _tenant(tenant: object) -> str:
    """A tenant name, checked (the runtime trusts the API with tenancy, not with input)."""
    from .auth import TRUSTED, AuthError
    if tenant is not None and not isinstance(tenant, str):
        raise InvalidRequest("'tenant' must be a string")
    try:
        return TRUSTED.resolve_tenant(tenant)
    except AuthError as e:
        raise InvalidRequest(e.detail) from e


def _tenants(tenants: object) -> tuple[str, ...] | None:
    if tenants is None:
        return None
    if not isinstance(tenants, (list, tuple)) or any(not isinstance(t, str) for t in tenants):
        raise InvalidRequest("'tenants' must be a list of tenant names")
    return tuple(tenants)


class LocalServices:
    """The daemon's operations, run where its VMs are (see the module doc)."""

    def __init__(self, daemon: FlintDaemon) -> None:
        self._daemon = daemon
        self._build_lock = threading.RLock()
        self._builds: dict[str, threading.Thread] = {}

    # The daemon creates these while it starts, after this object.
    @property
    def tokens(self):
        return self._daemon.tokens

    @property
    def eventlog(self):
        return self._daemon.eventlog

    @property
    def cluster_mode(self) -> bool:
        return self._daemon.cluster is not None

    @property
    def _manager(self):
        return self._daemon.manager

    @property
    def _backend(self):
        return self._daemon.backend

    def start(self, loop) -> None:
        if self._daemon.metrics is not None:
            self._daemon.metrics.start(loop)

    # ── The host ─────────────────────────────────────────────────────────

    @operation
    def health(self) -> dict:
        d = self._daemon
        result = {"status": "ok", "golden_snapshot_ready": d.golden_ready, "backend_kind": d.backend.kind,
                  "network_policy": d.backend.supports_network_policy}
        if d.storage:
            result["storage_backend"] = d.storage.kind
            result["storage_healthy"] = d.storage.is_running()
        if d.cluster:
            result["cluster_host_id"] = d.cluster.host_id
            result["host_class"] = d.cluster.host_class
        return result

    @operation
    def get_config(self) -> dict:
        from flint.core import config
        current = {field: getattr(config, name) for field, (name, _type) in config.CONFIG_FIELDS.items()}
        return {"config": current, "overrides": _load_config_overrides()}

    @operation
    def patch_config(self, changes: dict) -> dict:
        """Change some of the settings ``config.CONFIG_FIELDS`` lists; they apply at the next start."""
        from flint.core.config import CONFIG_FIELDS, CONFIG_OVERRIDES_PATH
        if not isinstance(changes, dict):
            raise InvalidRequest("Expected an object of settings")
        overrides = _load_config_overrides()
        for key, value in changes.items():
            meta = CONFIG_FIELDS.get(key)
            if not meta:
                raise InvalidRequest(f"Unknown field: {key}")
            _name, typ = meta
            try:
                overrides[key] = typ(value)
            except (TypeError, ValueError):
                raise InvalidRequest(f"Invalid type for {key}") from None
        os.makedirs(os.path.dirname(CONFIG_OVERRIDES_PATH), exist_ok=True)
        tmp_path = CONFIG_OVERRIDES_PATH + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(overrides, f)
        os.rename(tmp_path, CONFIG_OVERRIDES_PATH)
        return {"ok": True, "overrides": overrides, "requires_restart": list(changes)}

    # ── Quotas ───────────────────────────────────────────────────────────

    def _usage(self, tenant: str) -> Usage:
        """What a tenant holds on this host: its VMs (paused too) and volumes."""
        from flint.core._quotas import Usage
        vms = sum(1 for d in self._manager.list_dicts() if d.get("tenant") == tenant)
        vcpus, memory = self._backend.vm_resources()
        gib = sum(v.size_gib or 0 for v in self._daemon.volumes.list((tenant,)))
        return Usage(vms, vms * vcpus, vms * memory, gib)

    def _reserve(self, tenant: str, want: Usage):
        """Hold ``want`` against ``tenant``'s quota while it's created (a cluster's control plane enforces its own)."""
        if self.cluster_mode:
            return contextlib.nullcontext()
        return self._daemon.quotas.reserve(tenant, lambda: self._usage(tenant), want)

    @operation
    def get_quota(self, tenant: str) -> dict:
        return self._daemon.quotas.view(tenant, self._usage(tenant))

    @operation
    def set_quota(self, tenant: str, changes: dict) -> dict:
        self._daemon.quotas.set(tenant, changes)
        return self.get_quota(tenant)

    # ── VMs ──────────────────────────────────────────────────────────────

    @operation
    def list_vms(self) -> list[dict]:
        return self._manager.list_dicts()

    @operation
    def get_vm(self, vm_id: str) -> dict | None:
        return self._manager.get_dict(vm_id)

    def _require_running(self, vm_id: str):
        entry = self._manager.get_entry(vm_id)
        if entry is None:
            raise SandboxNotFound.of(vm_id)
        return entry

    @operation
    def create_vm(self, *, template_id: str = DEFAULT_TEMPLATE_ID, tenant: str = "", allow_internet_access: bool = True,
                  use_pool: bool = True, network_policy: Any = None, timeout_seconds: float | None = None,
                  timeout_policy: str = "kill", vm_id: str | None = None, availability: str = "",
                  snapshot: dict | None = None, template_snapshot: dict | None = None,
                  replace_shared_disk: bool = False) -> dict:
        """Start a VM from a template and return it.

        A cluster's control plane also passes ``vm_id`` (it assigns IDs),
        ``availability``, ``snapshot`` (restore a paused, checkpointed or
        forked VM from the snapshot store instead of booting the template)
        and ``template_snapshot`` (where to fetch a template this host
        doesn't have yet).
        """
        from flint.core._quotas import Usage
        from flint.core._template_registry import _slugify, template_usable
        from flint.core.manager import check_timeout
        d, cluster = self._daemon, self._daemon.cluster
        _flag("allow_internet_access", allow_internet_access)
        _flag("use_pool", use_pool)
        _flag("replace_shared_disk", replace_shared_disk)
        policy = _policy(network_policy)
        if timeout_seconds:
            check_timeout(timeout_seconds, timeout_policy or "kill")  # before the VM exists, not after
        if not isinstance(template_id, str) or not template_id:
            raise InvalidRequest("Invalid template ID")
        if not cluster:
            if vm_id or snapshot or template_snapshot or availability or replace_shared_disk:
                raise InvalidRequest("vm_id, availability and snapshots are only accepted from a cluster control plane")
            if _slugify(template_id) != template_id:
                raise InvalidRequest("Invalid template ID")
            tenant = _tenant(tenant)
            template = self.get_template(template_id)
            if template is None and template_id != DEFAULT_TEMPLATE_ID:
                raise NotFound(f"Template '{template_id}' not found")
            if template is not None and not template_usable(template, self._backend.kind):
                raise Conflict("Template is not ready")
        if template_id == DEFAULT_TEMPLATE_ID and not d.golden_ready and not snapshot:
            raise Unavailable("Golden snapshot not ready")
        if cluster:
            from flint.cluster.agent import capacity
            running = sum(1 for e in self._manager.entries() if e.state != SandboxState.PAUSED)
            if running >= capacity()["max_vms"]:
                raise HostFull("host is full")

        def start() -> str:
            if snapshot:
                return cluster.restore_vm(
                    vm_id, snapshot, template_id=template_id, allow_internet_access=allow_internet_access,
                    network_policy=policy, tenant=tenant, availability=availability,
                )
            if template_snapshot:
                cluster.ensure_template(template_id, template_snapshot)
            return self._manager.create(
                template_id=template_id, allow_internet_access=allow_internet_access, use_pool=use_pool,
                network_policy=policy, vm_id=vm_id, tenant=tenant, availability=availability,
            )

        vcpus, memory = self._backend.vm_resources()
        try:
            with self._reserve(tenant, Usage(1, vcpus, memory)):
                from flint.core._shared_rootfs import disk_replacement
                with disk_replacement(replace_shared_disk and cluster is not None):
                    vm_id = start()
                if cluster:
                    try:
                        cluster.fence_started(vm_id)
                    except Exception as e:
                        self.kill_vm(vm_id, missing_ok=True)
                        raise _Unfenced(f"VM {vm_id[:8]} not fenced, killed it: {e}") from e
        except FlintError as e:
            log.warning("creating a VM failed: %s", e, exc_info=isinstance(e, _Unfenced))
            raise
        except Exception as e:
            log.exception("creating a VM failed")
            raise FlintError(f"VM creation failed: {e}") from e

        self._setup_storage(vm_id, template_id)
        if timeout_seconds:
            self._manager.set_timeout(vm_id, timeout_seconds, timeout_policy or "kill")
        result = self._manager.get_dict(vm_id) or {"vm_id": vm_id}
        if d.storage:
            result["storage_backend"] = d.storage.kind
            result["workspace_dir"] = WORKSPACE_DIR
        d.write_state()
        events.publish("vm.created", vm=result)
        how = f"snapshot '{snapshot['id']}'" if snapshot else f"template '{template_id}'"
        _logs.append(vm_id, f"VM created from {how} in {result.get('ready_time_ms') or 0:.0f} ms")
        for step, ms in (result.get("timings") or {}).items():
            _logs.append(vm_id, f"  timing.{step} = {ms:.1f} ms")
        log.info("created VM %s from %s", vm_id[:8], how)
        return result

    def _setup_storage(self, vm_id: str, template_id: str) -> None:
        """Connect the new sandbox's workspace to the storage backend (nothing for local storage)."""
        storage, entry = self._daemon.storage, self._manager.get_entry(vm_id)
        if not storage or not entry:
            return
        try:
            storage.setup_sandbox(vm_id=vm_id, template_id=template_id, veth_ip=entry.backend_metadata.get("veth_ip", ""),
                                  ns_name=entry.ns_name, agent_url=entry.agent_url)
        except Exception as e:
            log.warning("Storage setup failed for %s: %s", vm_id[:8], e)

    @operation
    def kill_vm(self, vm_id: str, missing_ok: bool = False) -> None:
        """Delete a sandbox (running or paused) and everything it holds."""
        from flint.core._wipe import WipeError
        if not missing_ok and self._manager.get_dict(vm_id) is None:
            raise SandboxNotFound.of(vm_id)
        try:
            self._manager.kill(vm_id)
        except WipeError as e:
            raise Unavailable(str(e)) from e
        if self._daemon.cluster:
            self._daemon.cluster.forget_pause(vm_id)
        self._daemon.write_state()
        events.publish("vm.deleted", vm_id=vm_id)
        _logs.append(vm_id, "VM killed")
        log.info("killed VM %s", vm_id[:8])

    @operation
    def pause_vm(self, vm_id: str, snapshot_id: str | None = None, upload: str = "sync") -> dict:
        """Pause. In a cluster, ``snapshot_id`` also exports the paused state to
        the snapshot store so the VM can be resumed on any host."""
        cluster = self._daemon.cluster
        if snapshot_id and not cluster:
            raise InvalidRequest("snapshot export needs cluster mode")

        def pause() -> dict | None:
            if not snapshot_id:
                self._manager.pause(vm_id)
                return None
            with cluster.api_pause(vm_id):
                self._manager.pause(vm_id)
                try:
                    return cluster.export_pause(vm_id, snapshot_id, upload=upload or "sync")
                except Exception:
                    # The control plane will treat this pause as failed: leave the VM
                    # running, as it believes, rather than paused with no snapshot.
                    log.exception("[%s] pause export failed; resuming", vm_id[:8])
                    self._manager.resume(vm_id)
                    raise

        snap = _attempt("Pause", vm_id, pause)
        self._daemon.write_state()
        events.publish("vm.paused", vm_id=vm_id)
        _logs.append(vm_id, "VM paused")
        log.info("paused VM %s", vm_id[:8])
        return {"ok": True, "snapshot": snap} if snap else {"ok": True}

    @operation
    def resume_vm(self, vm_id: str) -> dict:
        cluster = self._daemon.cluster
        _attempt("Resume", vm_id, lambda: self._manager.resume(vm_id))
        if cluster:
            try:
                cluster.fence_started(vm_id)
            except Exception as e:
                # Running unfenced could leave two copies; paused, it's safe here.
                log.exception("[%s] resumed but not fenced; pausing it again", vm_id[:8])
                self._manager.pause(vm_id)
                raise Unavailable(f"VM resumed but not fenced, paused it again: {e}") from e
            cluster.forget_pause(vm_id)
        result = self._manager.get_dict(vm_id) or {"vm_id": vm_id}
        self._daemon.write_state()
        events.publish("vm.resumed", vm=result)
        _logs.append(vm_id, "VM resumed")
        log.info("resumed VM %s", vm_id[:8])
        return result

    @operation
    def checkpoint_vm(self, vm_id: str, snapshot_id: str, kind: str = "checkpoint", parent: str = "",
                      upload: str = "sync") -> dict:
        """Cluster only: snapshot a running VM without stopping it (fork, durable VMs)."""
        cluster = self._daemon.cluster
        if not cluster:
            raise InvalidRequest("checkpoints need cluster mode")
        if not snapshot_id:
            raise InvalidRequest("snapshot_id is required")
        self._require_running(vm_id)
        result = _attempt("Checkpoint", vm_id, lambda: cluster.checkpoint(
            vm_id, snapshot_id, kind or "checkpoint", parent=parent or "", upload=upload or "sync"))
        events.publish("vm.checkpoint", vm_id=vm_id, snapshot_id=snapshot_id, timings=result.get("timings"))
        return result

    @operation
    def live_migration(self, vm_id: str, action: str, body: dict) -> dict:
        """One step of moving a running VM between cluster hosts (driven by the control plane)."""
        cluster = self._daemon.cluster
        if not cluster:
            raise Conflict("live migration requires a cluster host")
        migration = cluster.migration
        steps = {"prepare": lambda: migration.prepare(vm_id, body), "send": lambda: migration.send(vm_id, body),
                 "status": lambda: migration.status(vm_id), "abort": lambda: migration.abort(vm_id),
                 "rollback": lambda: migration.rollback(vm_id), "commit": lambda: migration.commit(vm_id)}
        if action not in steps:
            raise Conflict("unknown migration action")
        try:
            return steps[action]()
        except Exception as e:
            raise Conflict(str(e)) from e

    @operation
    def set_timeout(self, vm_id: str, timeout_seconds: float, policy: str = "kill") -> None:
        self._manager.set_timeout(vm_id, timeout_seconds, policy)

    @operation
    def get_network_policy(self, vm_id: str) -> dict | None:
        """The sandbox's network policy. Credential values are write-only: redacted here."""
        self._require_running(vm_id)
        return self._manager.get_network_policy(vm_id)

    @operation
    def set_network_policy(self, vm_id: str, policy: Any) -> None:
        """Replace the network policy (credential injection rules) of a running sandbox."""
        self._require_running(vm_id)
        parsed = _policy(policy)
        if parsed is None:
            raise InvalidRequest("Network policy must be a JSON object")
        if not self._backend.supports_network_policy:
            raise Unsupported(f"Network policy isn't supported on the {self._backend.name} backend")
        self._manager.update_network_policy(vm_id, parsed)
        log.info("updated the network policy of VM %s", vm_id[:8])

    @operation
    def vm_metrics(self, vm_id: str, window: int = 60) -> list[dict]:
        if self._daemon.metrics is None:
            raise Unavailable("Metrics sampler not initialized")
        self._require_running(vm_id)
        return self._daemon.metrics.get(vm_id, window=window)

    @operation
    def connect_agent(self, vm_id: str):
        """A socket starting to connect to the VM's guest agent.

        Returns with the connection still in progress: a guest that drops the
        SYN must not hold a request slot. The privileged runtime connects only
        to this VM's fixed agent port; the API parses everything the guest sends.
        """
        return self._manager.connect_agent(vm_id)

    # ── Volumes ──────────────────────────────────────────────────────────

    @operation
    def list_mounts(self, vm_id: str) -> list[dict]:
        return [m.to_dict() for m in self._daemon.volumes.list_mounts(vm_id)]

    @operation
    def mount_volume(self, vm_id: str, volume: str, path: str, subpath: str = "", readonly: bool = False,
                     exclusive: bool = False) -> dict:
        """Mount a volume (or a subpath of it) into a running sandbox."""
        entry = self._manager.get_entry(vm_id)
        if entry and Maintenance.of(entry.backend_metadata).root_disk:
            raise Conflict("host-local volumes are incompatible with migration networking")
        m = self._daemon.volumes.mount(vm_id, volume, path, subpath, _flag("readonly", readonly),
                                       _flag("exclusive", exclusive))
        events.publish("volume.attached", vm_id=vm_id, volume_id=m.volume_id, mount_path=m.path)
        mode = "exclusive" if m.exclusive else ("ro" if m.readonly else "rw")
        _logs.append(vm_id, f"Mounted volume {m.volume_id}:/{m.subpath} at {m.path} ({mode})")
        return m.to_dict()

    @operation
    def unmount_volume(self, vm_id: str, path: str) -> None:
        m = self._daemon.volumes.unmount(vm_id, path)
        events.publish("volume.detached", vm_id=vm_id, volume_id=m.volume_id, mount_path=m.path)
        _logs.append(vm_id, f"Unmounted {m.path}")

    @operation
    def list_volumes(self, tenants: list[str] | None = None) -> list[dict]:
        """Volumes of ``tenants`` (None: every tenant's)."""
        return [v.to_dict() for v in self._daemon.volumes.list(_tenants(tenants))]

    @operation
    def create_volume(self, name: str, size_gib: int | None = None, tenant: str = "default") -> dict:
        from flint.core import _nfs_server
        from flint.core._quotas import Usage
        from flint.core._volumes import DEFAULT_SIZE_GIB
        tenant = _tenant(tenant)
        # Only block images have a size to count against the quota; the volume checks size_gib itself.
        size = (size_gib or DEFAULT_SIZE_GIB) if _nfs_server.images_available() else 0
        if type(size) is not int or size < 0:
            raise InvalidRequest("'size_gib' must be an integer")
        with self._reserve(tenant, Usage(volume_gib=size)):
            vol = self._daemon.volumes.create(name, size_gib, tenant).to_dict()
        events.publish("volume.created", volume=vol)
        log.info("created volume %s (%s)", vol["id"], vol["name"])
        return vol

    def _copy_volume(self, ref: str, name: str | None, tenants: list[str] | None, readonly: bool) -> dict:
        """Snapshot or fork: a copy counts its full size against the tenant's quota."""
        from flint.core._quotas import Usage
        volumes, scope = self._daemon.volumes, _tenants(tenants)
        src = volumes.require(ref, scope)
        copy = volumes.snapshot if readonly else volumes.fork
        with self._reserve(src.tenant, Usage(volume_gib=src.size_gib or 0)):
            vol = copy(ref, name, scope).to_dict()
        events.publish("volume.created", volume=vol)
        log.info("%s volume %s as %s (%s)", "snapshot" if readonly else "forked", ref, vol["id"], vol["name"])
        return vol

    @operation
    def snapshot_volume(self, ref: str, name: str | None = None, tenants: list[str] | None = None) -> dict:
        """A read-only copy of the volume as it is now."""
        return self._copy_volume(ref, name, tenants, readonly=True)

    @operation
    def fork_volume(self, ref: str, name: str | None = None, tenants: list[str] | None = None) -> dict:
        """A writable copy of the volume as it is now."""
        return self._copy_volume(ref, name, tenants, readonly=False)

    @operation
    def delete_volume(self, ref: str, tenants: list[str] | None = None) -> None:
        vol = self._daemon.volumes.delete(ref, _tenants(tenants))
        events.publish("volume.deleted", volume_id=vol.id, tenant=vol.tenant)
        log.info("deleted volume %s", vol.id)

    # ── Templates ────────────────────────────────────────────────────────

    @operation
    def list_templates(self) -> list[dict]:
        from flint.core._template_registry import list_templates
        return list_templates()

    @operation
    def get_template(self, template_id: str) -> dict | None:
        from flint.core._template_registry import get_template
        return get_template(template_id)

    def _building(self, template_id: str) -> bool:
        thread = self._builds.get(template_id)
        return thread is not None and thread.is_alive()

    @operation
    def build_template(self, name: str, dockerfile: str, rootfs_size_mb: int = 500, tenant: str = "") -> dict:
        """Build a template from a Dockerfile, in the background; it belongs to ``tenant``
        ("" keeps a template shared before tenancy shared). Poll it with get_template."""
        from flint.core._build_job import Admission, BuildBusy
        from flint.core._template_registry import _slugify, claim_template, update_template_artifact_status
        from flint.core.errors import Busy
        if not isinstance(name, str) or not name or not isinstance(dockerfile, str) or not dockerfile:
            raise InvalidRequest("name and dockerfile are required")
        if type(rootfs_size_mb) is not int or not 64 <= rootfs_size_mb <= 16384:
            raise InvalidRequest("rootfs_size_mb must be an integer between 64 and 16384")
        template_id = _slugify(name)
        if template_id == DEFAULT_TEMPLATE_ID:
            raise InvalidRequest("The default template can't be rebuilt this way")
        cluster, kind = self._daemon.cluster, self._backend.kind
        with self._build_lock:
            if self._building(template_id):
                raise Conflict("A build for this template is already running")
            if not cluster:
                old = self.get_template(template_id)
                # "" only rebuilds a template that was shared before tenancy.
                if tenant != "" or not old or old.get("tenant"):
                    tenant = _tenant(tenant)
                if old and old.get("tenant", "") != tenant:
                    raise Forbidden("Template belongs to another tenant")
            try:
                admission = Admission()
            except BuildBusy as e:
                raise Busy(str(e), headers={"Retry-After": "5"}) from e
            try:
                if cluster:
                    cluster.forget_template(template_id)
                else:
                    claim_template(template_id, name, tenant)
                # A polling client must see this attempt before its worker starts,
                # rather than returning the previous attempt's ready/failed result.
                update_template_artifact_status(template_id, kind, "building")
            except Exception:
                admission.release()
                raise

            def run() -> None:
                try:
                    with admission:
                        self._backend.build_template(name, dockerfile, rootfs_size_mb=rootfs_size_mb)
                    log.info("template %s built", template_id)
                except Exception as e:
                    update_template_artifact_status(template_id, kind, "failed", error=str(e))
                    log.exception("Template build failed: %s", template_id)
                finally:
                    with self._build_lock:
                        self._builds.pop(template_id, None)

            thread = threading.Thread(target=run, daemon=True, name=f"build-{template_id}")
            self._builds[template_id] = thread
            try:
                thread.start()
            except Exception as e:
                admission.release()
                self._builds.pop(template_id, None)
                update_template_artifact_status(template_id, kind, "failed", error=str(e))
                raise
        log.info("building template %s", template_id)
        return {"template_id": template_id, "status": "building"}

    @operation
    def delete_template(self, template_id: str) -> None:
        from flint.core._template_registry import delete_template
        if template_id == DEFAULT_TEMPLATE_ID:
            raise InvalidRequest("Cannot delete default template")
        with self._build_lock:
            if self._building(template_id):
                raise Conflict("Cannot delete a template while its build is running")
            template = self.get_template(template_id)
            if template is None:
                raise NotFound("Template not found")
            if self._daemon.cluster:
                self._daemon.cluster.forget_template(template_id)
            self._backend.delete_template_artifact(template_id, template)
            for artifact in (template.get("artifacts") or {}).values():
                directory = artifact.get("template_dir")
                if directory and os.path.isdir(directory):
                    shutil.rmtree(directory, ignore_errors=True)
            delete_template(template_id)
        log.info("deleted template %s", template_id)

    # ── Tenants ──────────────────────────────────────────────────────────

    @operation
    def get_tenant_network(self, tenant: str) -> bool:
        """Whether the tenant's VMs can reach each other."""
        return self._daemon.state_store.tenant_network(tenant)

    @operation
    def set_tenant_network(self, tenant: str, enabled: bool) -> None:
        self._manager.set_tenant_network(tenant, _flag("allow_same_tenant", enabled))


def _attempt(what: str, vm_id: str, fn):
    """Run a VM operation; an unexpected failure becomes a 500 that says what failed."""
    try:
        return fn()
    except FlintError:
        raise
    except Exception as e:
        log.exception("[%s] %s failed", vm_id[:8], what.lower())
        raise FlintError(f"{what} failed: {e}") from e


def _load_config_overrides() -> dict:
    from flint.core.config import CONFIG_OVERRIDES_PATH
    try:
        with open(CONFIG_OVERRIDES_PATH) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}

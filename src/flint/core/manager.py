"""The sandbox manager: every sandbox's lifecycle on this host, and its record of it.

The manager owns sandbox state. Backends drive the hypervisor (start, pause,
resume, stop a VM); the manager decides when, tracks the running sandboxes in
memory, and records every transition in the :class:`StateStore`. Requests it
can't serve raise :mod:`flint.core.errors`.
"""

from __future__ import annotations

import functools
import json
import os
import stat
import threading
import time
import weakref
from typing import TYPE_CHECKING, Callable

from . import _wipe, config
from .backends.base import BackendPlugin
from .config import DEFAULT_TEMPLATE_ID, log
from .errors import Conflict, InvalidState, SandboxNotFound
from .events import EventSink, discard
from .guest import GuestAgent, bridge_terminal
from .policy import NetworkPolicy, redacted
from .types import BootResult, SandboxRow, SandboxState, _SandboxEntry

if TYPE_CHECKING:
    from ._state_store import StateStore


def _vm_operation(method):
    @functools.wraps(method)
    def run(self, *args, **kwargs):
        vm_id = args[0] if args else kwargs.get("sandbox_id", kwargs.get("vm_id"))
        with self._operation_lock(vm_id):
            return method(self, *args, **kwargs)
    return run


class SandboxManager:
    """Owns all sandbox state and lifecycle. No TUI dependencies."""

    def __init__(self, backend: BackendPlugin, state_store: StateStore | None = None, *, publish: EventSink = discard) -> None:
        self._backend = backend
        self._sandboxes: dict[str, _SandboxEntry] = {}
        self._lock = threading.Lock()
        self._delete_lock = threading.RLock()
        self._network_lock = threading.RLock()
        self._operation_locks = weakref.WeakValueDictionary()
        self._state_store = state_store
        self._publish = publish
        # Called with the sandbox ID just before a sandbox is killed.
        self.kill_hooks: list[Callable[[str], None]] = []

    @property
    def backend(self) -> BackendPlugin:
        return self._backend

    @property
    def backend_kind(self) -> str:
        return self._backend.kind

    def _operation_lock(self, vm_id: str):
        with self._lock:
            lock = self._operation_locks.get(vm_id)
            if lock is None:
                lock = threading.RLock()
                self._operation_locks[vm_id] = lock
            return lock

    # ── Tracking ───────────────────────────────────────────────────────────

    def entries(self) -> list[_SandboxEntry]:
        """The sandboxes whose VMs this manager tracks (running, or held by a migration)."""
        with self._lock:
            return list(self._sandboxes.values())

    def track(self, entry: _SandboxEntry) -> None:
        """Track a sandbox whose VM is already recorded (e.g. adopted by recovery)."""
        with self._lock:
            self._sandboxes[entry.vm_id] = entry

    def untrack(self, sandbox_id: str) -> None:
        with self._lock:
            self._sandboxes.pop(sandbox_id, None)

    def adopt(self, entry: _SandboxEntry) -> None:
        """Record and track a sandbox whose VM was started outside :meth:`create` (a migration receiver)."""
        self._insert(entry)
        self.track(entry)

    def save(self, entry: _SandboxEntry, *, state: SandboxState | None = None) -> None:
        """Record a tracked sandbox's metadata and availability, and its new ``state`` if given."""
        if state is not None:
            entry.state = state
        if self._state_store:
            self._state_store.update_sandbox(
                entry.vm_id, backend_meta_json=json.dumps(entry.backend_metadata), availability=entry.availability,
            )
            if state is not None:
                self._state_store.transition_state(entry.vm_id, state)

    def _entry(self, sandbox_id: str) -> _SandboxEntry:
        """A tracked sandbox. Raises InvalidState for one only on record (e.g. paused), else SandboxNotFound."""
        with self._lock:
            entry = self._sandboxes.get(sandbox_id)
        if entry:
            return entry
        row = self._state_store.get_sandbox(sandbox_id) if self._state_store else None
        if row and row.state != SandboxState.DEAD:
            raise InvalidState(f"Sandbox {sandbox_id} is not running (state={row.state})")
        raise SandboxNotFound.of(sandbox_id)

    def _running_entry(self, sandbox_id: str) -> _SandboxEntry:
        entry = self._entry(sandbox_id)
        if entry.state != SandboxState.RUNNING:
            raise InvalidState(f"Sandbox {sandbox_id} is not running (state={entry.state})")
        return entry

    def _insert(self, entry: _SandboxEntry) -> None:
        if self._state_store:
            self._state_store.insert_sandbox(
                vm_id=entry.vm_id,
                pid=entry.pid,
                vm_dir=entry.vm_dir,
                socket_path=entry.socket_path,
                ns_name=entry.ns_name,
                state=entry.state,
                daemon_pid=os.getpid(),
                template_id=entry.template_id,
                boot_time_ms=entry.ready_time_ms,
                timings_json=entry.timings,
                chroot_base=entry.chroot_base,
                backend_kind=entry.backend_kind,
                backend_meta_json=entry.backend_metadata,
                tenant=entry.tenant,
                availability=entry.availability,
            )

    # ── Tenant networks ────────────────────────────────────────────────────

    def refresh_tenant_networks(self) -> None:
        if not self._backend.supports_tenant_networks or not self._state_store:
            return
        from .net.netns import configure_tenant_peers
        with self._network_lock:
            groups: dict[tuple[str, str], list[str]] = {}
            for row in self._state_store.list_active():
                if not row.ns_name or row.state in (SandboxState.ERROR, SandboxState.DELETING):
                    continue
                tenant = row.get("tenant") or "default"
                key = ("tenant", tenant) if self._state_store.tenant_network(tenant) else ("vm", row.ns_name)
                groups.setdefault(key, []).append(row.ns_name)
            configure_tenant_peers(list(groups.values()))

    def set_tenant_network(self, tenant: str, enabled: bool) -> None:
        if not self._backend.supports_tenant_networks or config.CLUSTER_MODE:
            raise ValueError("Tenant networking requires a single Linux host")
        with self._network_lock:
            self._state_store.set_tenant_network(tenant, enabled)
            self.refresh_tenant_networks()

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def create(self, *, template_id: str = DEFAULT_TEMPLATE_ID, allow_internet_access: bool = True, use_pool: bool = True,
               use_pyroute2: bool = True, network_policy: NetworkPolicy | dict | None = None, vm_id: str | None = None,
               tenant: str = "", availability: str = "") -> str:
        """Start an interactive VM from a template snapshot. Returns the vm_id.

        ``vm_id`` is chosen by the caller in a cluster (the control plane),
        so a VM keeps its ID when it moves between hosts.
        """
        policy = NetworkPolicy.optional(network_policy)
        if config.SHARED_ROOTFS_DIR:
            if not config.CLUSTER_MODE:
                raise ValueError("shared root disks require cluster ownership and fencing")
            if not self._backend.supports_live_migration:
                raise ValueError("shared root disks require Cloud Hypervisor")
            if policy or config.STORAGE_BACKEND != "local":
                raise ValueError("migration networking requires rootfs-local workspaces and no host-local credential proxy")
        boot = self._backend.create(
            template_id=template_id,
            allow_internet_access=allow_internet_access,
            use_pool=use_pool,
            use_pyroute2=use_pyroute2,
            vm_id=vm_id,
        )
        return self._register_boot(boot, template_id, policy, tenant, availability)

    @_vm_operation
    def restore(self, vm_id: str, src_dir: str, *, template_id: str = DEFAULT_TEMPLATE_ID, allow_internet_access: bool = True,
                network_policy: NetworkPolicy | dict | None = None, tenant: str = "", availability: str = "") -> str:
        """Start ``vm_id`` from a snapshot of another VM (paused, checkpointed or
        forked, possibly on another host) restored into ``src_dir``."""
        policy = NetworkPolicy.optional(network_policy)
        with self._lock:
            if vm_id in self._sandboxes:
                raise Conflict(f"Sandbox {vm_id} already runs here")
        if self._state_store:
            row = self._state_store.get_sandbox(vm_id)
            if row and row.state != SandboxState.DEAD:
                self.kill(vm_id)  # a stale local copy (e.g. paused here before it moved)
        boot = self._backend.import_snapshot(vm_id, src_dir, allow_internet_access=allow_internet_access)
        return self._register_boot(boot, template_id, policy, tenant, availability)

    @_vm_operation
    def checkpoint(self, sandbox_id: str, snapshot: Callable[[str, dict[str, str]], object]) -> object:
        """Snapshot a running sandbox without stopping it (see BackendPlugin.checkpoint)."""
        return self._backend.checkpoint(self._running_entry(sandbox_id), snapshot)

    def _register_boot(self, boot: BootResult, template_id: str, policy: NetworkPolicy | None, tenant: str, availability: str) -> str:
        with self._operation_lock(boot.vm_id):
            return self._register_boot_locked(boot, template_id, policy, tenant, availability)

    def _register_boot_locked(self, boot: BootResult, template_id: str, policy: NetworkPolicy | None, tenant: str, availability: str) -> str:
        vm_id = boot.vm_id
        entry = _SandboxEntry.from_boot(
            boot, kind=self._backend.kind, template_id=template_id,
            tenant=tenant or "default", availability=availability, network_policy=policy,
        )
        entry.ready_time_ms = (time.monotonic() - boot.t_total) * 1000
        vcpus, memory_mb = self._backend.vm_resources()
        entry.backend_metadata["resources"] = {"vcpus": vcpus, "memory_mb": memory_mb, "disk_mb": _disk_mb(boot.vm_dir)}

        # Warmup: send a quick exec to measure time-to-interactive.
        t0 = time.monotonic()
        try:
            self._backend.guest(entry).exec(["echo", "benchmark"], timeout=5, request_timeout=10)
        except Exception as exc:
            log.warning("[%s] warm-up exec failed: %s", vm_id[:8], exc)
        entry.timings["exec_command_ms"] = (time.monotonic() - t0) * 1000

        with self._lock:
            self._sandboxes[vm_id] = entry
        self._insert(entry)
        if policy and self._state_store:
            self._state_store.set_network_policy(vm_id, policy.to_json_text())

        # Start credential proxy if network policy has transforms
        if policy:
            try:
                self._apply_network_policy(entry, policy)
            except Exception:
                self.kill(vm_id)
                raise

        self.refresh_tenant_networks()

        total_ms = (time.monotonic() - boot.t_total) * 1000
        parts = " | ".join(f"{k}={v:.1f}" for k, v in boot.timings.items())
        log.debug("[%s] DONE %.0f ms: %s", vm_id[:8], total_ms, parts)
        return vm_id

    @_vm_operation
    def kill(self, sandbox_id: str) -> None:
        with self._delete_lock:
            self._kill(sandbox_id)

    def _kill(self, sandbox_id: str) -> None:
        with self._lock:
            entry = self._sandboxes.get(sandbox_id)
            if entry:
                # Under the lock: the health monitor must not overwrite this.
                entry.state = SandboxState.DELETING
                entry.agent_healthy = False
        row = self._state_store.get_sandbox(sandbox_id) if self._state_store else None
        if not entry and (not row or row.state == SandboxState.DEAD):
            return
        if self._state_store:
            if row and row.state != SandboxState.DELETING:
                self._state_store.transition_state(sandbox_id, SandboxState.DELETING, detail="deletion pending")
            self._state_store.update_sandbox(sandbox_id, deletion_attempt_at=time.time())
        try:
            # Stop the VMM first: a firewall failure must not prevent that.
            stopped = self._backend.force_stop(
                sandbox_id,
                pid=entry.pid if entry else row.pid,
                root=entry.vm_dir if entry else row.vm_dir,
                process=entry.process if entry else None,
            )
            if stopped:
                if entry:
                    entry.process = None
                    entry.pid = 0
                if self._state_store:
                    self._state_store.update_sandbox(sandbox_id, pid=0)
            # Revoke peer addresses before any namespace/address is released.
            self.refresh_tenant_networks()
            if entry and entry.proxy:
                entry.proxy.stop()
                entry.proxy = None
            elif self._backend.supports_network_policy:
                from .net.proxy import stop_namespace_proxy
                stop_namespace_proxy(entry.ns_name if entry else row.ns_name)
            for hook in self.kill_hooks:
                hook(sandbox_id)
            if entry:
                self._backend.kill(entry)
            elif row:
                self._backend.discard_paused(SandboxRow({**row, "pid": 0}))
        except Exception as exc:
            if self._state_store:
                self._state_store.update_sandbox(sandbox_id, error_message=str(exc))
            raise _wipe.WipeError(f"Deletion pending for {sandbox_id}; retry cleanup: {exc}") from exc
        self.untrack(sandbox_id)
        if self._state_store:
            self._state_store.transition_state(sandbox_id, SandboxState.DEAD)
        self._report_wiped(sandbox_id)

    def _report_wiped(self, sandbox_id: str) -> None:
        """Publish ``vm.wiped``: proof that the sandbox's files were wiped before removal."""
        wiped = _wipe.take(sandbox_id)
        log.info("[%s] wiped %d files (%d bytes)", sandbox_id[:8], wiped["files"], wiped["bytes"])
        try:
            self._publish("vm.wiped", vm_id=sandbox_id, files=wiped["files"], bytes=wiped["bytes"])
        except Exception:
            log.exception("[%s] publishing vm.wiped failed", sandbox_id[:8])

    @_vm_operation
    def pause(self, sandbox_id: str) -> None:
        """Pause a running sandbox: snapshot its state to disk and stop its VMM."""
        entry = self._running_entry(sandbox_id)
        pause_ref = self._backend.pause(entry)

        entry.state = SandboxState.PAUSED
        entry.agent_healthy = False
        entry.process = None
        if self._state_store:
            self._state_store.transition_state(sandbox_id, SandboxState.PAUSED)
            self._state_store.set_pause_snapshot(sandbox_id, pause_ref)

        if entry.proxy:
            entry.proxy.stop()
            entry.proxy = None
        self.untrack(sandbox_id)
        log.debug("[%s] paused", sandbox_id[:8])

    @_vm_operation
    def resume(self, sandbox_id: str) -> str:
        """Resume a paused sandbox from its snapshot."""
        if not self._state_store:
            raise RuntimeError("StateStore required for resume")

        row = self._state_store.get_sandbox(sandbox_id)
        if not row:
            raise SandboxNotFound.of(sandbox_id, " in state store")
        if row.state != SandboxState.PAUSED:
            raise InvalidState(f"Sandbox {sandbox_id} is not paused (state={row['state']})")

        boot = self._backend.resume(row)
        policy = NetworkPolicy.from_json_text(self._state_store.get_network_policy(sandbox_id))
        entry = _SandboxEntry.from_boot(
            boot, kind=row.get("backend_kind") or self._backend.kind,
            template_id=row.get("template_id") or DEFAULT_TEMPLATE_ID,
            tenant=row.get("tenant") or "", availability=row.get("availability") or "",
            network_policy=policy,
        )
        # The API reports the timings of a VM's creation; a resumed one has none.
        entry.timings = {}
        resources = row.backend_metadata.get("resources")
        if resources is not None:
            entry.backend_metadata.setdefault("resources", resources)
        self.track(entry)

        self._state_store.transition_state(sandbox_id, SandboxState.RUNNING)
        self._state_store.update_sandbox(
            sandbox_id,
            pid=boot.pid,
            daemon_pid=os.getpid(),
            backend_meta_json=json.dumps(entry.backend_metadata),
        )

        # Re-apply credential injection proxy if policy has transforms
        if policy:
            self._apply_network_policy(entry, policy)

        self.refresh_tenant_networks()

        log.debug("[%s] resumed", sandbox_id[:8])
        return sandbox_id

    def set_timeout(self, sandbox_id: str, timeout_seconds: float, policy: str = "kill") -> None:
        """Set or update the timeout for a sandbox."""
        if not self._state_store:
            raise RuntimeError("StateStore required for set_timeout")
        timeout_at = time.time() + timeout_seconds
        self._state_store.set_timeout(sandbox_id, timeout_at, policy)
        with self._lock:
            entry = self._sandboxes.get(sandbox_id)
        if entry:
            entry.timeout_seconds = timeout_seconds
            entry.timeout_policy = policy

    # ── Views ──────────────────────────────────────────────────────────────

    def list_dicts(self) -> list[dict]:
        """Return JSON-serializable dicts for all VMs, paused ones included.

        Pausing drops a VM from ``_sandboxes`` (its VMM is gone), so paused VMs
        come from the state store, the same way ``get_dict`` finds them.
        """
        with self._lock:
            dicts = [entry.to_dict() for entry in self._sandboxes.values()]
            live = set(self._sandboxes)
        if self._state_store:
            for row in self._state_store.list_active():
                if row.vm_id not in live:
                    dicts.append(self._row_dict(row))
        return dicts

    def get_dict(self, sandbox_id: str) -> dict | None:
        """Return JSON-serializable dict for a single VM, or None."""
        with self._lock:
            entry = self._sandboxes.get(sandbox_id)
        if entry is not None:
            return entry.to_dict()
        # Fallback: check state store for paused/persisted VMs
        if self._state_store:
            row = self._state_store.get_sandbox(sandbox_id)
            if row:
                return self._row_dict(row)
        return None

    def _row_dict(self, row: SandboxRow) -> dict:
        """VM dict for a VM known only to the state store (e.g. paused)."""
        timings = {}
        if row.get("timings_json"):
            try:
                timings = json.loads(row["timings_json"])
            except (json.JSONDecodeError, TypeError):
                log.warning("[%s] unreadable timings in the state store", row.vm_id[:8])
        return {
            "vm_id": row.vm_id,
            "pid": row["pid"],
            "state": row["state"],
            "template_id": row.get("template_id", DEFAULT_TEMPLATE_ID),
            "backend_kind": row.get("backend_kind") or self._backend.kind,
            "agent_healthy": False,
            "created_at": row["created_at"],
            "boot_time_ms": row.get("boot_time_ms"),
            "ready_time_ms": None,
            "timings": timings,
            "log_lines": [],
            "line_count": 0,
            "network_policy": None,
            "timeout_seconds": None,
            "timeout_policy": row.get("timeout_policy"),
            "tenant": row.get("tenant") or "",
            "network_address": row.backend_metadata.get("veth_ip", ""),
            "availability": row.get("availability") or "",
        }

    def get_entry(self, sandbox_id: str) -> _SandboxEntry | None:
        """Return the raw entry for a VM. None if not found."""
        with self._lock:
            entry = self._sandboxes.get(sandbox_id)
            return entry if entry and entry.state != SandboxState.DELETING else None

    def vm_ids(self) -> list[str]:
        """Return list of all VM IDs."""
        with self._lock:
            return list(self._sandboxes.keys())

    # ── Network policy ─────────────────────────────────────────────────────

    @_vm_operation
    def update_network_policy(self, sandbox_id: str, policy: NetworkPolicy | dict) -> None:
        """Replace the network policy (credential injection rules) of a sandbox.

        A header value sent back in the redacted form the API returns keeps
        the current credential, so a client can edit a policy it can't read.
        """
        entry = self._running_entry(sandbox_id)
        policy = NetworkPolicy.parse(policy).keeping_credentials_of(entry.network_policy)

        # Apply first: a policy that can't be applied isn't recorded.
        self._apply_network_policy(entry, policy)
        entry.network_policy = policy

        if self._state_store:
            self._state_store.set_network_policy(sandbox_id, policy.to_json_text())

    def get_network_policy(self, sandbox_id: str) -> dict | None:
        """The sandbox's network policy, with credential values redacted."""
        return redacted(self._entry(sandbox_id).network_policy)

    def restore_network_policy(self, sandbox_id: str) -> None:
        """Re-apply a recovered sandbox's stored policy: its credential proxy didn't survive the restart."""
        entry = self.get_entry(sandbox_id)
        policy = NetworkPolicy.from_json_text(self._state_store.get_network_policy(sandbox_id)) if self._state_store else None
        if entry and policy:
            entry.network_policy = policy
            self._apply_network_policy(entry, policy)

    def _apply_network_policy(self, entry: _SandboxEntry, policy: NetworkPolicy) -> None:
        """Start, update, or stop the credential proxy based on the policy."""
        from .net.netns import _remove_proxy_redirect, _setup_proxy_redirect
        from .net.proxy import CredentialProxy

        if policy.has_transforms:
            if entry.proxy:
                entry.proxy.update_rules(policy.headers)
            else:
                proxy = CredentialProxy(entry.ns_name, entry.vm_id)
                proxy.start(policy.headers)
                entry.proxy = proxy
                _setup_proxy_redirect(entry.ns_name)
                self._trust_proxy_ca(entry, proxy)
        elif entry.proxy:
            entry.proxy.stop()
            entry.proxy = None
            _remove_proxy_redirect(entry.ns_name)

    def _trust_proxy_ca(self, entry: _SandboxEntry, proxy) -> None:
        """Add the proxy's CA to the guest's system trust bundle.

        Credentials are only injected over TLS, which the proxy terminates
        with certificates from its CA; without this, TLS clients in the guest
        reject the connection. Idempotent, and the guest disk keeps it across
        pause/resume. Only the public certificate leaves the host.
        """
        # The proxy owns its CA directory. Never follow a certificate symlink
        # it could plant to make the root supervisor read a host secret.
        from ._jailfs import _open_nofollow
        fd = _open_nofollow(proxy.ca_cert_path, os.O_RDONLY, (stat.S_IFREG,))
        with os.fdopen(fd) as f:
            cert = f.read(65537).strip()
        if len(cert) > 65536:
            raise ValueError("Proxy CA certificate is too large")
        script = (
            'mkdir -p /etc/ssl/certs && cd /etc/ssl/certs || exit 1\n'
            '[ "$(cat flint-proxy-ca.pem 2>/dev/null)" = "$1" ] && exit 0\n'
            'printf "%s\\n" "$1" > flint-proxy-ca.pem && printf "%s\\n" "$1" >> ca-certificates.crt\n'
        )
        result = self._backend.guest(entry).sh(script, cert, timeout=10, request_timeout=15)
        if not result.ok:
            raise RuntimeError(f"Installing the proxy CA in the guest failed: {result.body[:300]!r}")

    # ── The guest agent ────────────────────────────────────────────────────

    def connect_agent(self, sandbox_id: str):
        """A socket starting to connect to the sandbox's guest agent."""
        entry = self.get_entry(sandbox_id)
        if not entry:
            raise SandboxNotFound.of(sandbox_id)
        return self._backend.connect_guest(entry)

    def guest(self, sandbox_id: str) -> GuestAgent:
        """A client for the sandbox's guest agent."""
        if not self.get_entry(sandbox_id):
            raise SandboxNotFound.of(sandbox_id)
        return GuestAgent(lambda: self.connect_agent(sandbox_id))

    async def bridge_terminal(self, sandbox_id: str, websocket) -> None:
        await bridge_terminal(self.guest(sandbox_id), websocket)


def _disk_mb(vm_dir: str) -> int:
    try:
        return (os.stat(os.path.join(vm_dir, "rootfs.ext4")).st_size + (1 << 20) - 1) // (1 << 20)
    except OSError:
        return 0

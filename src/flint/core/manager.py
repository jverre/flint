from __future__ import annotations

import json
import os
import threading
import time
from typing import TYPE_CHECKING

from .backends.base import HostBackend
from .config import log, DEFAULT_TEMPLATE_ID
from .types import _SandboxEntry, SandboxState

if TYPE_CHECKING:
    from ._state_store import StateStore


def _policy_has_transforms(policy: dict) -> bool:
    """Check if a network policy contains any transform rules."""
    allow = policy.get("allow", {})
    for domain_rules in allow.values():
        for rule in domain_rules:
            if rule.get("transform"):
                return True
    return False


def _extract_transform_rules(policy: dict) -> dict:
    """Extract domain -> headers mapping from a network policy.

    Returns a dict like: {"api.openai.com": {"Authorization": "Bearer sk-..."}, ...}
    """
    rules: dict[str, dict[str, str]] = {}
    allow = policy.get("allow", {})
    for domain, domain_rules in allow.items():
        merged_headers: dict[str, str] = {}
        for rule in domain_rules:
            for transform in rule.get("transform", []):
                merged_headers.update(transform.get("headers", {}))
        if merged_headers:
            rules[domain] = merged_headers
    return rules


class SandboxManager:
    """Owns all sandbox state and lifecycle. No TUI dependencies."""

    def __init__(self, backend: HostBackend, state_store: StateStore | None = None) -> None:
        self._backend = backend
        self._sandboxes: dict[str, _SandboxEntry] = {}
        self._lock = threading.Lock()
        self._state_store = state_store

    def create(self, *, template_id: str = DEFAULT_TEMPLATE_ID, allow_internet_access: bool = True, use_pool: bool = True, use_pyroute2: bool = True, network_policy: dict | None = None) -> str:
        """Start an interactive VM from a template snapshot. Returns the vm_id."""
        boot = self._backend.create(
            template_id=template_id,
            allow_internet_access=allow_internet_access,
            use_pool=use_pool,
            use_pyroute2=use_pyroute2,
        )

        vm_id = boot.backend_vm_ref or os.path.basename(boot.runtime_dir) or str(time.time())
        agent_url = boot.agent_url

        entry = _SandboxEntry(
            vm_id=vm_id,
            process=boot.process,
            pid=boot.pid,
            vm_dir=boot.vm_dir,
            socket_path=boot.socket_path,
            ns_name=boot.ns_name,
            guest_ip=boot.guest_ip,
            agent_url=agent_url,
            agent_healthy=True,
            state=SandboxState.RUNNING,
            template_id=template_id,
            chroot_base=boot.chroot_base,
            backend_kind=self._backend.kind,
            backend_vm_ref=boot.backend_vm_ref or vm_id,
            runtime_dir=boot.runtime_dir or boot.vm_dir,
            guest_arch=boot.guest_arch,
            transport_ref=boot.transport_ref,
            backend_metadata=dict(boot.backend_metadata),
            t_instance_start=boot.t_total,
            ready_time_ms=(time.monotonic() - boot.t_total) * 1000,
            timings=boot.timings,
            network_policy=network_policy,
        )

        # Warmup: send a quick exec to measure time-to-interactive.
        t0 = time.monotonic()
        try:
            body = json.dumps({"cmd": ["echo", "benchmark"], "timeout": 5}).encode()
            self._backend.proxy_guest_request(entry, "POST", "/exec", body, timeout=10)
        except Exception:
            pass
        entry.timings["exec_command_ms"] = (time.monotonic() - t0) * 1000

        with self._lock:
            self._sandboxes[vm_id] = entry

        # Persist to state store if available
        if self._state_store:
            self._state_store.insert_sandbox(
                vm_id=vm_id,
                pid=boot.process.pid,
                vm_dir=boot.vm_dir,
                socket_path=boot.socket_path,
                ns_name=boot.ns_name,
                state=SandboxState.RUNNING,
                daemon_pid=os.getpid(),
                template_id=template_id,
                boot_time_ms=entry.ready_time_ms,
                timings_json=entry.timings,
                chroot_base=boot.chroot_base,
                backend_kind=entry.backend_kind,
                backend_vm_ref=entry.backend_vm_ref,
                runtime_dir=entry.runtime_dir,
                guest_arch=entry.guest_arch,
                transport_ref=entry.transport_ref,
                backend_meta_json=entry.backend_metadata,
            )

        # Persist network policy if provided
        if network_policy and self._state_store:
            self._state_store.set_network_policy(vm_id, json.dumps(network_policy))

        # Start credential proxy if network policy has transforms
        if network_policy:
            self._apply_network_policy(entry, network_policy)

        total_ms = (time.monotonic() - boot.t_total) * 1000
        parts = " | ".join(f"{k}={v:.1f}" for k, v in boot.timings.items())
        log.debug("[%s] DONE %.0f ms: %s", vm_id[:8], total_ms, parts)

        return vm_id

    def kill(self, sandbox_id: str) -> None:
        with self._lock:
            entry = self._sandboxes.pop(sandbox_id, None)
        if not entry:
            return

        # Stop credential proxy before tearing down the VM
        if entry.proxy:
            try:
                entry.proxy.stop()
            except Exception:
                pass

        self._backend.kill(entry)

        if self._state_store:
            self._state_store.transition_state(sandbox_id, SandboxState.DEAD)

    def pause(self, sandbox_id: str) -> None:
        """Pause a running sandbox: snapshot state to disk, kill process."""
        from ._firecracker import _fc_patch, _fc_put, _fc_status_ok

        with self._lock:
            entry = self._sandboxes.get(sandbox_id)
        if not entry:
            raise RuntimeError(f"Sandbox {sandbox_id} not found")
        if entry.state != SandboxState.RUNNING:
            raise RuntimeError(f"Sandbox {sandbox_id} is not running (state={entry.state})")

        self._backend.pause(entry, self._state_store)

        with self._lock:
            self._sandboxes.pop(sandbox_id, None)

        log.debug("[%s] paused", sandbox_id[:8])

    def resume(self, sandbox_id: str) -> str:
        """Resume a paused sandbox from its snapshot."""
        if not self._state_store:
            raise RuntimeError("StateStore required for resume")

        row = self._state_store.get_sandbox(sandbox_id)
        if not row:
            raise RuntimeError(f"Sandbox {sandbox_id} not found in state store")
        if row["state"] != SandboxState.PAUSED.value:
            raise RuntimeError(f"Sandbox {sandbox_id} is not paused (state={row['state']})")

        boot = self._backend.resume(row)

        # 6. Restore network policy from SQLite
        network_policy = None
        policy_json = self._state_store.get_network_policy(sandbox_id)
        if policy_json:
            try:
                network_policy = json.loads(policy_json)
            except (json.JSONDecodeError, TypeError):
                pass

        # 7. Create entry and insert into in-memory dict
        entry = _SandboxEntry(
            vm_id=sandbox_id,
            process=boot.process,
            pid=boot.pid,
            vm_dir=boot.vm_dir,
            socket_path=boot.socket_path,
            ns_name=boot.ns_name,
            guest_ip=boot.guest_ip,
            agent_url=boot.agent_url,
            agent_healthy=True,
            state=SandboxState.RUNNING,
            template_id=row.get("template_id", DEFAULT_TEMPLATE_ID),
            chroot_base=boot.chroot_base,
            network_policy=network_policy,
            backend_kind=row.get("backend_kind") or self._backend.kind,
            backend_vm_ref=boot.backend_vm_ref or sandbox_id,
            runtime_dir=boot.runtime_dir or boot.vm_dir,
            guest_arch=boot.guest_arch or row.get("guest_arch") or "",
            transport_ref=boot.transport_ref or row.get("transport_ref") or "",
            backend_metadata=dict(boot.backend_metadata),
        )

        with self._lock:
            self._sandboxes[sandbox_id] = entry

        # 8. Persist state transition
        if self._state_store:
            self._state_store.transition_state(sandbox_id, SandboxState.RUNNING)
            self._state_store.update_sandbox(
                sandbox_id,
                pid=boot.pid,
                daemon_pid=os.getpid(),
                runtime_dir=entry.runtime_dir,
                guest_arch=entry.guest_arch,
                transport_ref=entry.transport_ref,
                backend_meta_json=json.dumps(entry.backend_metadata),
            )

        # 9. Re-apply credential injection proxy if policy has transforms
        if network_policy:
            self._apply_network_policy(entry, network_policy)

        log.debug("[%s] resumed", sandbox_id[:8])
        return sandbox_id

    def set_timeout(self, sandbox_id: str, timeout_seconds: float, policy: str = "kill") -> None:
        """Set or update the timeout for a sandbox."""
        if not self._state_store:
            raise RuntimeError("StateStore required for set_timeout")
        timeout_at = time.time() + timeout_seconds
        self._state_store.set_timeout(sandbox_id, timeout_at, policy)

    def list_dicts(self) -> list[dict]:
        """Return JSON-serializable dicts for all VMs."""
        with self._lock:
            return [entry.to_dict() for entry in self._sandboxes.values()]

    def get_dict(self, sandbox_id: str) -> dict | None:
        """Return JSON-serializable dict for a single VM, or None."""
        with self._lock:
            entry = self._sandboxes.get(sandbox_id)
        if entry is None:
            return None
        return entry.to_dict()

    def get_entry(self, sandbox_id: str) -> _SandboxEntry | None:
        """Return the raw entry for a VM. None if not found."""
        with self._lock:
            return self._sandboxes.get(sandbox_id)

    def vm_ids(self) -> list[str]:
        """Return list of all VM IDs."""
        with self._lock:
            return list(self._sandboxes.keys())

    def update_network_policy(self, sandbox_id: str, policy: dict) -> None:
        """Update the network policy (credential injection rules) for a sandbox."""
        with self._lock:
            entry = self._sandboxes.get(sandbox_id)
        if not entry:
            raise RuntimeError(f"Sandbox {sandbox_id} not found")

        entry.network_policy = policy

        if self._state_store:
            self._state_store.set_network_policy(sandbox_id, json.dumps(policy))

        self._apply_network_policy(entry, policy)

    def get_network_policy(self, sandbox_id: str) -> dict | None:
        """Get the current network policy for a sandbox."""
        with self._lock:
            entry = self._sandboxes.get(sandbox_id)
        if not entry:
            raise RuntimeError(f"Sandbox {sandbox_id} not found")
        return entry.network_policy

    def _apply_network_policy(self, entry: _SandboxEntry, policy: dict) -> None:
        """Start, update, or stop the credential proxy based on the policy."""
        from ._proxy import CredentialProxy
        from ._netns import _setup_proxy_redirect, _remove_proxy_redirect

        has_transforms = _policy_has_transforms(policy)

        if has_transforms:
            rules = _extract_transform_rules(policy)
            if entry.proxy:
                entry.proxy.update_rules(rules)
            else:
                proxy = CredentialProxy(entry.ns_name)
                proxy.start(rules)
                entry.proxy = proxy
                _setup_proxy_redirect(entry.ns_name)
        else:
            if entry.proxy:
                entry.proxy.stop()
                entry.proxy = None
                _remove_proxy_redirect(entry.ns_name)

    @property
    def backend_kind(self) -> str:
        return self._backend.kind

    def agent_request(
        self,
        sandbox_id: str,
        method: str,
        path: str,
        body: bytes | None = None,
        timeout: float = 65,
    ) -> tuple[int, bytes]:
        entry = self.get_entry(sandbox_id)
        if not entry:
            raise RuntimeError(f"Sandbox {sandbox_id} not found")
        return self._backend.proxy_guest_request(entry, method, path, body, timeout)

    async def bridge_terminal(self, sandbox_id: str, websocket) -> None:
        entry = self.get_entry(sandbox_id)
        if not entry:
            raise RuntimeError(f"Sandbox {sandbox_id} not found")
        await self._backend.bridge_terminal(entry, websocket)

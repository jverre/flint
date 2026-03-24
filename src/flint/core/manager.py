from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from typing import TYPE_CHECKING

from .config import log, GOLDEN_DIR, GOLDEN_TAP, GUEST_IP, DEFAULT_TEMPLATE_ID
from .types import _SandboxEntry, SandboxState
from ._boot import _boot_from_snapshot, _teardown_vm, BootResult
from ._netns import _enter_netns, _restore_netns
from ._snapshot import golden_snapshot_exists
from ._template_registry import template_snapshot_exists as _template_snapshot_exists

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

    def __init__(self, state_store: StateStore | None = None) -> None:
        self._sandboxes: dict[str, _SandboxEntry] = {}
        self._lock = threading.Lock()
        self._state_store = state_store

    def create(self, *, template_id: str = DEFAULT_TEMPLATE_ID, allow_internet_access: bool = True, use_pool: bool = True, use_pyroute2: bool = True, network_policy: dict | None = None) -> str:
        """Start an interactive VM from a template snapshot. Returns the vm_id."""
        if template_id == DEFAULT_TEMPLATE_ID:
            if not golden_snapshot_exists():
                raise RuntimeError(f"Golden snapshot not found in {GOLDEN_DIR}")
        else:
            if not _template_snapshot_exists(template_id):
                raise RuntimeError(f"Template snapshot not found: {template_id}")

        boot = _boot_from_snapshot(
            template_id=template_id,
            allow_internet_access=allow_internet_access,
            use_pool=use_pool,
            use_pyroute2=use_pyroute2,
            network_overrides=[{"iface_id": "eth0", "host_dev_name": GOLDEN_TAP}],
        )

        vm_id = boot.vm_id
        agent_url = boot.agent_url

        # Warmup: send a quick exec to measure time-to-interactive
        t0 = time.monotonic()
        try:
            self._agent_exec(boot.ns_name, agent_url, ["echo", "benchmark"], timeout=5)
        except Exception:
            pass
        boot.timings["exec_command_ms"] = (time.monotonic() - t0) * 1000

        entry = _SandboxEntry(
            vm_id=vm_id,
            process=boot.process,
            pid=boot.process.pid,
            vm_dir=boot.vm_dir,
            socket_path=boot.socket_path,
            ns_name=boot.ns_name,
            guest_ip=GUEST_IP,
            agent_url=agent_url,
            agent_healthy=True,
            state=SandboxState.RUNNING,
            template_id=template_id,
            chroot_base=boot.chroot_base,
            t_instance_start=boot.t_total,
            ready_time_ms=(time.monotonic() - boot.t_total) * 1000,
            timings=boot.timings,
            network_policy=network_policy,
        )

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
                timings_json=boot.timings,
                chroot_base=boot.chroot_base,
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

        _teardown_vm(entry.process, entry.ns_name, entry.chroot_base, entry.vm_id)

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

        # 1. Pause vCPU
        _fc_patch(entry.socket_path, "/vm", {"state": "Paused"})

        # 2. Create pause snapshot (paths are chroot-relative)
        snapshot_body = {
            "snapshot_type": "Full",
            "snapshot_path": "pause-vmstate",
            "mem_file_path": "pause-mem",
        }
        resp = _fc_put(entry.socket_path, "/snapshot/create", snapshot_body)
        if not _fc_status_ok(resp):
            # Resume VM on failure
            _fc_patch(entry.socket_path, "/vm", {"state": "Resumed"})
            raise RuntimeError(f"Snapshot create failed: {resp}")

        # 3. Kill Firecracker process (snapshot is on disk)
        if entry.process:
            entry.process.kill()
            try:
                entry.process.wait(timeout=2)
            except Exception:
                pass

        # 4. Update state
        entry.state = SandboxState.PAUSED
        entry.agent_healthy = False
        entry.process = None

        # 5. Persist
        if self._state_store:
            self._state_store.transition_state(sandbox_id, SandboxState.PAUSED)
            self._state_store.set_pause_snapshot(sandbox_id, entry.vm_dir)

        # 6. Remove from in-memory dict (no active process to track)
        with self._lock:
            self._sandboxes.pop(sandbox_id, None)

        log.debug("[%s] paused", sandbox_id[:8])

    def resume(self, sandbox_id: str) -> str:
        """Resume a paused sandbox from its snapshot."""
        from ._firecracker import _fc_put, _fc_patch, _fc_status_ok, _wait_for_api_socket, _wait_for_agent
        from ._jailer import JailSpec, build_jailer_command
        import subprocess as _subprocess

        if not self._state_store:
            raise RuntimeError("StateStore required for resume")

        row = self._state_store.get_sandbox(sandbox_id)
        if not row:
            raise RuntimeError(f"Sandbox {sandbox_id} not found in state store")
        if row["state"] != SandboxState.PAUSED.value:
            raise RuntimeError(f"Sandbox {sandbox_id} is not paused (state={row['state']})")

        vm_dir = row["vm_dir"]
        ns_name = row["ns_name"]
        chroot_base = row["chroot_base"]

        spec = JailSpec(vm_id=sandbox_id, ns_name=ns_name)
        socket_path = spec.socket_path_on_host

        # Remove stale socket from previous run
        try:
            os.unlink(socket_path)
        except FileNotFoundError:
            pass

        # 1. Start new jailer in existing netns
        log_path = f"{vm_dir}/firecracker.log"
        with open(log_path, "w") as log_fd:
            process = _subprocess.Popen(
                build_jailer_command(spec),
                stdin=_subprocess.DEVNULL, stdout=log_fd, stderr=_subprocess.STDOUT,
                start_new_session=True,
            )

        try:
            _wait_for_api_socket(socket_path)

            # 2. Load pause snapshot (chroot-relative paths)
            snapshot_body = {
                "snapshot_path": "pause-vmstate",
                "mem_backend": {"backend_type": "File", "backend_path": "pause-mem"},
                "enable_diff_snapshots": False,
                "resume_vm": False,
            }
            resp = _fc_put(socket_path, "/snapshot/load", snapshot_body)
            if not _fc_status_ok(resp):
                raise RuntimeError(f"snapshot/load failed: {resp}")

            # 3. Patch drives (chroot-relative)
            _fc_patch(socket_path, "/drives/rootfs", {"drive_id": "rootfs", "path_on_host": "rootfs.ext4"})

            # 4. Resume VM
            _fc_patch(socket_path, "/vm", {"state": "Resumed"})

            # 5. Wait for guest agent
            agent_url = _wait_for_agent(ns_name)
        except Exception:
            process.kill()
            try:
                process.wait(timeout=2)
            except Exception:
                pass
            raise

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
            process=process,
            pid=process.pid,
            vm_dir=vm_dir,
            socket_path=socket_path,
            ns_name=ns_name,
            guest_ip=GUEST_IP,
            agent_url=agent_url,
            agent_healthy=True,
            state=SandboxState.RUNNING,
            template_id=row.get("template_id", DEFAULT_TEMPLATE_ID),
            chroot_base=chroot_base,
            network_policy=network_policy,
        )

        with self._lock:
            self._sandboxes[sandbox_id] = entry

        # 8. Persist state transition
        if self._state_store:
            self._state_store.transition_state(sandbox_id, SandboxState.RUNNING)
            self._state_store.update_sandbox(sandbox_id, pid=process.pid, daemon_pid=os.getpid())

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

    @staticmethod
    def _agent_exec(ns_name: str, agent_url: str, cmd: list[str], timeout: float = 60) -> dict:
        """Execute a command on the guest agent via HTTP, entering the namespace."""
        body = json.dumps({"cmd": cmd, "timeout": int(timeout)}).encode()
        url = f"{agent_url}/exec"
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")

        orig_fd = _enter_netns(ns_name)
        try:
            with urllib.request.urlopen(req, timeout=timeout + 5) as resp:
                return json.loads(resp.read())
        finally:
            _restore_netns(orig_fd)

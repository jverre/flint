from __future__ import annotations

import json
import os
import threading
import time
from typing import TYPE_CHECKING

from flint.errors import BackendCapabilityMissing, SandboxNotFound

from .backends import default_backend_kind, get_backend
from .backends.base import Backend
from .backends.capabilities import (
    EvalRequest,
    EvalResult,
    ExecRequest,
    ExecResult,
    FetchRequest,
    FetchResponse,
    FileInfo,
    ProcessHandle,
    ProcessSpec,
    SupportsFiles,
    SupportsJsEval,
    SupportsKv,
    SupportsMediatedFetch,
    SupportsPause,
    SupportsPool,
    SupportsPty,
    SupportsShell,
    SupportsTemplateBuild,
)
from .config import DEFAULT_TEMPLATE_ID, log
from .types import _SandboxEntry, SandboxState

if TYPE_CHECKING:
    from ._state_store import StateStore


def _policy_has_transforms(policy: dict) -> bool:
    allow = policy.get("allow", {})
    for domain_rules in allow.values():
        for rule in domain_rules:
            if rule.get("transform"):
                return True
    return False


def _extract_transform_rules(policy: dict) -> dict:
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
    """Owns sandbox state and dispatches operations to per-kind backends.

    A single manager handles multiple backend kinds. The backend instance for a
    given sandbox is determined by ``entry.backend_kind``. Backends are created
    lazily via :func:`flint.core.backends.get_backend` and cached.
    """

    def __init__(self, state_store: StateStore | None = None) -> None:
        self._sandboxes: dict[str, _SandboxEntry] = {}
        self._lock = threading.Lock()
        self._state_store = state_store
        self._backends: dict[str, Backend] = {}
        self._default_kind: str | None = None

    # ── Backend resolution ──────────────────────────────────────────────

    def backend_for(self, kind: str) -> Backend:
        b = self._backends.get(kind)
        if b is None:
            b = get_backend(kind)
            self._backends[kind] = b
        return b

    def backend_for_entry(self, entry: _SandboxEntry) -> Backend:
        return self.backend_for(entry.backend_kind)

    @property
    def default_kind(self) -> str:
        if self._default_kind is None:
            self._default_kind = default_backend_kind()
        return self._default_kind

    @default_kind.setter
    def default_kind(self, kind: str) -> None:
        self._default_kind = kind

    def loaded_backends(self) -> dict[str, Backend]:
        return dict(self._backends)

    # ── Lifecycle ───────────────────────────────────────────────────────

    def create(
        self,
        *,
        backend: str | None = None,
        template_id: str = DEFAULT_TEMPLATE_ID,
        options: dict | None = None,
        network_policy: dict | None = None,
    ) -> str:
        kind = backend or self.default_kind
        b = self.backend_for(kind)
        boot = b.create(template_id=template_id, options=options or {})

        vm_id = boot.backend_vm_ref or os.path.basename(boot.runtime_dir) or str(time.time())

        entry = _SandboxEntry(
            vm_id=vm_id,
            process=boot.process,
            pid=boot.pid,
            vm_dir=boot.vm_dir,
            socket_path=boot.socket_path,
            ns_name=boot.ns_name,
            guest_ip=boot.guest_ip,
            agent_url=boot.agent_url,
            agent_healthy=True,
            state=SandboxState.RUNNING,
            template_id=template_id,
            chroot_base=boot.chroot_base,
            backend_kind=b.kind,
            backend_vm_ref=boot.backend_vm_ref or vm_id,
            runtime_dir=boot.runtime_dir or boot.vm_dir,
            guest_arch=boot.guest_arch,
            transport_ref=boot.transport_ref,
            backend_metadata=dict(boot.backend_metadata),
            t_instance_start=boot.t_total,
            ready_time_ms=(time.monotonic() - boot.t_total) * 1000 if boot.t_total else None,
            timings=boot.timings,
            network_policy=network_policy,
        )

        # Warmup ping to measure time-to-interactive — only if the backend has shell.
        if isinstance(b, SupportsShell):
            t0 = time.monotonic()
            try:
                b.exec(entry, ExecRequest(cmd=["echo", "benchmark"], timeout=5))
            except Exception:
                pass
            entry.timings["exec_command_ms"] = (time.monotonic() - t0) * 1000

        with self._lock:
            self._sandboxes[vm_id] = entry

        if self._state_store:
            self._state_store.insert_sandbox(
                vm_id=vm_id,
                pid=boot.pid,
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

        if network_policy and self._state_store:
            self._state_store.set_network_policy(vm_id, json.dumps(network_policy))

        if network_policy:
            self._apply_network_policy(entry, network_policy)

        if boot.t_total:
            total_ms = (time.monotonic() - boot.t_total) * 1000
            parts = " | ".join(f"{k}={v:.1f}" for k, v in boot.timings.items())
            log.debug("[%s] DONE %.0f ms: %s", vm_id[:8], total_ms, parts)

        return vm_id

    def kill(self, sandbox_id: str) -> None:
        with self._lock:
            entry = self._sandboxes.pop(sandbox_id, None)
        if not entry:
            return

        if entry.proxy:
            try:
                entry.proxy.stop()
            except Exception:
                pass

        self.backend_for_entry(entry).kill(entry)

        if self._state_store:
            self._state_store.transition_state(sandbox_id, SandboxState.DEAD)

    def pause(self, sandbox_id: str) -> None:
        entry = self._require_entry(sandbox_id)
        if entry.state != SandboxState.RUNNING:
            raise RuntimeError(f"Sandbox {sandbox_id} is not running (state={entry.state})")
        b = self.backend_for_entry(entry)
        if not isinstance(b, SupportsPause):
            raise BackendCapabilityMissing(capability="pause", backend=b.kind)

        b.pause(entry, self._state_store)

        with self._lock:
            self._sandboxes.pop(sandbox_id, None)

        log.debug("[%s] paused", sandbox_id[:8])

    def resume(self, sandbox_id: str) -> str:
        if not self._state_store:
            raise RuntimeError("StateStore required for resume")

        row = self._state_store.get_sandbox(sandbox_id)
        if not row:
            raise SandboxNotFound(sandbox_id)
        if row["state"] != SandboxState.PAUSED.value:
            raise RuntimeError(f"Sandbox {sandbox_id} is not paused (state={row['state']})")

        b = self.backend_for(row.get("backend_kind") or self.default_kind)
        if not isinstance(b, SupportsPause):
            raise BackendCapabilityMissing(capability="pause", backend=b.kind)

        boot = b.resume(row)

        network_policy = None
        policy_json = self._state_store.get_network_policy(sandbox_id)
        if policy_json:
            try:
                network_policy = json.loads(policy_json)
            except (json.JSONDecodeError, TypeError):
                pass

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
            backend_kind=b.kind,
            backend_vm_ref=boot.backend_vm_ref or sandbox_id,
            runtime_dir=boot.runtime_dir or boot.vm_dir,
            guest_arch=boot.guest_arch or row.get("guest_arch") or "",
            transport_ref=boot.transport_ref or row.get("transport_ref") or "",
            backend_metadata=dict(boot.backend_metadata),
        )

        with self._lock:
            self._sandboxes[sandbox_id] = entry

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

        if network_policy:
            self._apply_network_policy(entry, network_policy)

        log.debug("[%s] resumed", sandbox_id[:8])
        return sandbox_id

    def set_timeout(self, sandbox_id: str, timeout_seconds: float, policy: str = "kill") -> None:
        if not self._state_store:
            raise RuntimeError("StateStore required for set_timeout")
        timeout_at = time.time() + timeout_seconds
        self._state_store.set_timeout(sandbox_id, timeout_at, policy)

    # ── Introspection ───────────────────────────────────────────────────

    def list_dicts(self) -> list[dict]:
        with self._lock:
            return [self._entry_to_dict(e) for e in self._sandboxes.values()]

    def get_dict(self, sandbox_id: str) -> dict | None:
        with self._lock:
            entry = self._sandboxes.get(sandbox_id)
        if entry is None:
            return None
        return self._entry_to_dict(entry)

    def _entry_to_dict(self, entry: _SandboxEntry) -> dict:
        d = entry.to_dict()
        try:
            d["capabilities"] = sorted(self.backend_for_entry(entry).capabilities)
        except Exception:
            d["capabilities"] = []
        return d

    def get_entry(self, sandbox_id: str) -> _SandboxEntry | None:
        with self._lock:
            return self._sandboxes.get(sandbox_id)

    def vm_ids(self) -> list[str]:
        with self._lock:
            return list(self._sandboxes.keys())

    def _require_entry(self, sandbox_id: str) -> _SandboxEntry:
        entry = self.get_entry(sandbox_id)
        if not entry:
            raise SandboxNotFound(sandbox_id)
        return entry

    def _require_capability(self, entry: _SandboxEntry, capability: str, proto):
        b = self.backend_for_entry(entry)
        if not isinstance(b, proto):
            raise BackendCapabilityMissing(capability=capability, backend=b.kind)
        return b

    # ── Capability-typed dispatch ───────────────────────────────────────

    def exec(self, sandbox_id: str, request: ExecRequest) -> ExecResult:
        entry = self._require_entry(sandbox_id)
        b = self._require_capability(entry, "shell", SupportsShell)
        return b.exec(entry, request)

    def read_file(self, sandbox_id: str, path: str) -> bytes:
        entry = self._require_entry(sandbox_id)
        b = self._require_capability(entry, "files", SupportsFiles)
        return b.read_file(entry, path)

    def write_file(self, sandbox_id: str, path: str, data: bytes, mode: str = "0644") -> None:
        entry = self._require_entry(sandbox_id)
        b = self._require_capability(entry, "files", SupportsFiles)
        b.write_file(entry, path, data, mode)

    def stat_file(self, sandbox_id: str, path: str) -> FileInfo:
        entry = self._require_entry(sandbox_id)
        b = self._require_capability(entry, "files", SupportsFiles)
        return b.stat_file(entry, path)

    def list_files(self, sandbox_id: str, path: str) -> list[FileInfo]:
        entry = self._require_entry(sandbox_id)
        b = self._require_capability(entry, "files", SupportsFiles)
        return b.list_files(entry, path)

    def mkdir(self, sandbox_id: str, path: str, parents: bool = True) -> None:
        entry = self._require_entry(sandbox_id)
        b = self._require_capability(entry, "files", SupportsFiles)
        b.mkdir(entry, path, parents)

    def delete_file(self, sandbox_id: str, path: str, recursive: bool = False) -> None:
        entry = self._require_entry(sandbox_id)
        b = self._require_capability(entry, "files", SupportsFiles)
        b.delete_file(entry, path, recursive)

    def create_process(self, sandbox_id: str, spec: ProcessSpec) -> ProcessHandle:
        entry = self._require_entry(sandbox_id)
        b = self._require_capability(entry, "pty", SupportsPty)
        return b.create_process(entry, spec)

    def list_processes(self, sandbox_id: str) -> list[dict]:
        entry = self._require_entry(sandbox_id)
        b = self._require_capability(entry, "pty", SupportsPty)
        return b.list_processes(entry)

    def send_process_input(self, sandbox_id: str, pid: int, data: bytes) -> None:
        entry = self._require_entry(sandbox_id)
        b = self._require_capability(entry, "pty", SupportsPty)
        b.send_process_input(entry, pid, data)

    def signal_process(self, sandbox_id: str, pid: int, signal: int) -> None:
        entry = self._require_entry(sandbox_id)
        b = self._require_capability(entry, "pty", SupportsPty)
        b.signal_process(entry, pid, signal)

    def resize_process(self, sandbox_id: str, pid: int, cols: int, rows: int) -> None:
        entry = self._require_entry(sandbox_id)
        b = self._require_capability(entry, "pty", SupportsPty)
        b.resize_process(entry, pid, cols, rows)

    async def attach_terminal(self, sandbox_id: str, websocket) -> None:
        entry = self._require_entry(sandbox_id)
        b = self._require_capability(entry, "pty", SupportsPty)
        await b.attach_terminal(entry, websocket)

    def eval_js(self, sandbox_id: str, request: EvalRequest) -> EvalResult:
        entry = self._require_entry(sandbox_id)
        b = self._require_capability(entry, "js_eval", SupportsJsEval)
        return b.eval_js(entry, request)

    def fetch(self, sandbox_id: str, request: FetchRequest) -> FetchResponse:
        entry = self._require_entry(sandbox_id)
        b = self._require_capability(entry, "fetch", SupportsMediatedFetch)
        return b.fetch(entry, request)

    def kv_get(self, sandbox_id: str, key: str) -> bytes | None:
        entry = self._require_entry(sandbox_id)
        b = self._require_capability(entry, "kv", SupportsKv)
        return b.kv_get(entry, key)

    def kv_put(self, sandbox_id: str, key: str, value: bytes) -> None:
        entry = self._require_entry(sandbox_id)
        b = self._require_capability(entry, "kv", SupportsKv)
        b.kv_put(entry, key, value)

    def kv_delete(self, sandbox_id: str, key: str) -> None:
        entry = self._require_entry(sandbox_id)
        b = self._require_capability(entry, "kv", SupportsKv)
        b.kv_delete(entry, key)

    # ── Backend-fanout helpers ──────────────────────────────────────────

    def start_pools(self) -> None:
        for b in self._backends.values():
            if isinstance(b, SupportsPool):
                try:
                    b.start_pool()
                except Exception:
                    log.exception("start_pool failed for %s", b.kind)

    def stop_pools(self) -> None:
        for b in self._backends.values():
            if isinstance(b, SupportsPool):
                try:
                    b.stop_pool()
                except Exception:
                    log.exception("stop_pool failed for %s", b.kind)

    def template_builder_for(self, backend_kind: str) -> "SupportsTemplateBuild":
        b = self.backend_for(backend_kind)
        if not isinstance(b, SupportsTemplateBuild):
            raise BackendCapabilityMissing(capability="template_build", backend=b.kind)
        return b

    # ── Network policy ──────────────────────────────────────────────────

    def update_network_policy(self, sandbox_id: str, policy: dict) -> None:
        entry = self._require_entry(sandbox_id)
        entry.network_policy = policy
        if self._state_store:
            self._state_store.set_network_policy(sandbox_id, json.dumps(policy))
        self._apply_network_policy(entry, policy)

    def get_network_policy(self, sandbox_id: str) -> dict | None:
        entry = self._require_entry(sandbox_id)
        return entry.network_policy

    def _apply_network_policy(self, entry: _SandboxEntry, policy: dict) -> None:
        from ._netns import _remove_proxy_redirect, _setup_proxy_redirect
        from ._proxy import CredentialProxy

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

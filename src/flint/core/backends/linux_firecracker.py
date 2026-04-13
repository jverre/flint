from __future__ import annotations

import asyncio
import base64
import json
import os
import platform
import shutil
import threading
import urllib.parse
import urllib.request

import websockets.sync.client as ws_sync

from flint.core._boot import _RecoveredProcess, _boot_from_snapshot, _teardown_vm
from flint.core._firecracker import _fc_patch, _fc_put, _fc_status_ok, _wait_for_agent, _wait_for_api_socket
from flint.core._netns import _delete_netns, _ensure_bridge, _enter_netns, _restore_netns
from flint.core._pool import start_pool as _pool_start, stop_pool as _pool_stop
from flint.core._recovery import _probe_sandbox
from flint.core._snapshot import create_golden_snapshot, golden_snapshot_exists
from flint.core._template_build import build_template as _build_template
from flint.core._template_registry import (
    delete_template_artifact as _delete_template_artifact,
    register_template_artifact,
    template_snapshot_exists,
)
from flint.core.config import (
    AGENT_PORT,
    DEFAULT_TEMPLATE_ID,
    GOLDEN_DIR,
    GOLDEN_TAP,
    GUEST_IP,
)
from flint.core.types import _SandboxEntry, SandboxState

from .base import ALIVE, DEAD, PAUSED, Backend, BackendBootResult
from .capabilities import (
    ExecRequest,
    ExecResult,
    FileInfo,
    ProcessHandle,
    ProcessSpec,
)


class LinuxFirecrackerBackend(Backend):
    """Firecracker microVM + jailer + Linux network namespace.

    Provides shell, files, PTY, pause/resume, pool, and template build by
    proxying to the in-guest flintd HTTP agent.
    """

    kind = "linux-firecracker"

    # ── Lifecycle ────────────────────────────────────────────────────────

    def ensure_runtime_ready(self) -> None:
        _ensure_bridge()

    def ensure_default_template(self) -> None:
        if not golden_snapshot_exists():
            shutil.rmtree(GOLDEN_DIR, ignore_errors=True)
            create_golden_snapshot()
        register_template_artifact(
            DEFAULT_TEMPLATE_ID,
            "Default (Alpine)",
            self.kind,
            GOLDEN_DIR,
            status="ready",
        )

    def create(self, *, template_id: str, options=None) -> BackendBootResult:
        opts = dict(options or {})
        allow_internet_access = bool(opts.get("allow_internet_access", True))
        use_pool = bool(opts.get("use_pool", True))
        use_pyroute2 = bool(opts.get("use_pyroute2", True))

        if template_id == DEFAULT_TEMPLATE_ID:
            if not golden_snapshot_exists():
                raise RuntimeError(f"Golden snapshot not found in {GOLDEN_DIR}")
        elif not template_snapshot_exists(template_id, backend_kind=self.kind):
            raise RuntimeError(f"Template snapshot not found: {template_id}")

        boot = _boot_from_snapshot(
            template_id=template_id,
            allow_internet_access=allow_internet_access,
            use_pool=use_pool,
            use_pyroute2=use_pyroute2,
            network_overrides=[{"iface_id": "eth0", "host_dev_name": GOLDEN_TAP}],
        )
        return BackendBootResult(
            process=boot.process,
            pid=boot.process.pid,
            vm_dir=boot.vm_dir,
            socket_path=boot.socket_path,
            ns_name=boot.ns_name,
            guest_ip=GUEST_IP,
            agent_url=boot.agent_url,
            chroot_base=boot.chroot_base,
            backend_vm_ref=boot.vm_id,
            runtime_dir=boot.vm_dir,
            guest_arch=platform.machine().lower(),
            transport_ref=f"netns:{boot.ns_name}",
            timings=dict(boot.timings),
            t_total=boot.t_total,
            backend_metadata={
                "socket_path": boot.socket_path,
                "ns_name": boot.ns_name,
                "chroot_base": boot.chroot_base,
            },
        )

    def kill(self, entry: _SandboxEntry) -> None:
        _teardown_vm(entry.process, entry.ns_name, entry.chroot_base, entry.vm_id)

    def health(self, entry: _SandboxEntry) -> tuple[bool, str | None]:
        pid = entry.pid
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False, f"process {pid} not found"
        except PermissionError:
            pass
        return True, None

    def recover(self, row: dict) -> tuple[str, _SandboxEntry | None]:
        probe = _probe_sandbox(row)
        if probe == "paused":
            return PAUSED, None
        if probe != "alive":
            self._cleanup_dead_row(row)
            return DEAD, None

        vm_id = row["vm_id"]
        pid = row["pid"]
        ns_name = row["ns_name"]
        vm_dir = row["vm_dir"]

        try:
            agent_url = _wait_for_agent(ns_name, retries=50)
        except Exception:
            self._cleanup_dead_row(row)
            return DEAD, None

        entry = _SandboxEntry(
            vm_id=vm_id,
            process=_RecoveredProcess(pid),
            pid=pid,
            vm_dir=vm_dir,
            socket_path=row["socket_path"],
            ns_name=ns_name,
            guest_ip=row.get("guest_ip") or GUEST_IP,
            agent_url=agent_url,
            agent_healthy=True,
            state=SandboxState.RUNNING,
            template_id=row.get("template_id", DEFAULT_TEMPLATE_ID),
            chroot_base=row.get("chroot_base") or "",
            backend_kind=row.get("backend_kind") or self.kind,
            backend_vm_ref=row.get("backend_vm_ref") or vm_id,
            runtime_dir=row.get("runtime_dir") or vm_dir,
            guest_arch=row.get("guest_arch") or "linux",
            transport_ref=row.get("transport_ref") or f"netns:{ns_name}",
        )
        return ALIVE, entry

    # ── SupportsPool ─────────────────────────────────────────────────────

    def start_pool(self) -> None:
        _pool_start()

    def stop_pool(self) -> None:
        _pool_stop()

    # ── SupportsPause ────────────────────────────────────────────────────

    def pause(self, entry: _SandboxEntry, state_store) -> None:
        _fc_patch(entry.socket_path, "/vm", {"state": "Paused"})
        snapshot_body = {
            "snapshot_type": "Full",
            "snapshot_path": "pause-vmstate",
            "mem_file_path": "pause-mem",
        }
        resp = _fc_put(entry.socket_path, "/snapshot/create", snapshot_body)
        if not _fc_status_ok(resp):
            _fc_patch(entry.socket_path, "/vm", {"state": "Resumed"})
            raise RuntimeError(f"Snapshot create failed: {resp}")

        if entry.process:
            entry.process.kill()
            try:
                entry.process.wait(timeout=2)
            except Exception:
                pass

        entry.state = SandboxState.PAUSED
        entry.agent_healthy = False
        entry.process = None
        entry.pause_state_ref = entry.vm_dir

        if state_store:
            state_store.transition_state(entry.vm_id, SandboxState.PAUSED)
            state_store.set_pause_snapshot(entry.vm_id, entry.vm_dir)
            state_store.update_sandbox(entry.vm_id, pause_state_ref=entry.vm_dir)

    def resume(self, row: dict) -> BackendBootResult:
        from flint.core._jailer import JailSpec, build_jailer_command
        import subprocess

        sandbox_id = row["vm_id"]
        vm_dir = row["vm_dir"]
        ns_name = row["ns_name"]
        chroot_base = row.get("chroot_base") or ""

        spec = JailSpec(vm_id=sandbox_id, ns_name=ns_name)
        socket_path = spec.socket_path_on_host

        try:
            os.unlink(socket_path)
        except FileNotFoundError:
            pass

        log_path = f"{vm_dir}/firecracker.log"
        with open(log_path, "w") as log_fd:
            process = subprocess.Popen(
                build_jailer_command(spec),
                stdin=subprocess.DEVNULL,
                stdout=log_fd,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

        try:
            _wait_for_api_socket(socket_path)
            snapshot_body = {
                "snapshot_path": "pause-vmstate",
                "mem_backend": {"backend_type": "File", "backend_path": "pause-mem"},
                "enable_diff_snapshots": False,
                "resume_vm": False,
            }
            resp = _fc_put(socket_path, "/snapshot/load", snapshot_body)
            if not _fc_status_ok(resp):
                raise RuntimeError(f"snapshot/load failed: {resp}")
            _fc_patch(socket_path, "/drives/rootfs", {"drive_id": "rootfs", "path_on_host": "rootfs.ext4"})
            _fc_patch(socket_path, "/vm", {"state": "Resumed"})
            agent_url = _wait_for_agent(ns_name)
        except Exception:
            process.kill()
            try:
                process.wait(timeout=2)
            except Exception:
                pass
            raise

        return BackendBootResult(
            process=process,
            pid=process.pid,
            vm_dir=vm_dir,
            socket_path=socket_path,
            ns_name=ns_name,
            guest_ip=GUEST_IP,
            agent_url=agent_url,
            chroot_base=chroot_base,
            backend_vm_ref=sandbox_id,
            runtime_dir=vm_dir,
            guest_arch=row.get("guest_arch") or platform.machine().lower(),
            transport_ref=row.get("transport_ref") or f"netns:{ns_name}",
            backend_metadata={
                "socket_path": socket_path,
                "ns_name": ns_name,
                "chroot_base": chroot_base,
            },
        )

    # ── SupportsTemplateBuild ────────────────────────────────────────────

    def build_template(self, name: str, source, **kwargs) -> dict:
        rootfs_size_mb = int(kwargs.get("rootfs_size_mb", 500))
        template_id = _build_template(name, source, rootfs_size_mb=rootfs_size_mb)
        return {"template_id": template_id, "status": "building"}

    def delete_template_artifact(self, template_id: str, template: dict | None = None) -> None:
        if template is None:
            return
        _delete_template_artifact(template_id, self.kind)

    # ── SupportsShell ────────────────────────────────────────────────────

    def exec(self, entry: _SandboxEntry, request: ExecRequest) -> ExecResult:
        body = json.dumps({
            "cmd": request.cmd,
            "env": request.env or {},
            "cwd": request.cwd or "",
            "timeout": int(request.timeout),
        }).encode()
        status, resp = self._proxy(entry, "POST", "/exec", body, timeout=request.timeout + 5)
        return _decode_exec_result(status, resp)

    # ── SupportsFiles ────────────────────────────────────────────────────

    def read_file(self, entry: _SandboxEntry, path: str) -> bytes:
        status, resp = self._proxy(entry, "GET", f"/files?path={_q(path)}")
        if status != 200:
            raise _agent_error("read_file", status, resp)
        return resp

    def write_file(self, entry: _SandboxEntry, path: str, data: bytes, mode: str = "0644") -> None:
        status, resp = self._proxy(entry, "POST", f"/files?path={_q(path)}&mode={mode}", data)
        if status not in (200, 201):
            raise _agent_error("write_file", status, resp)

    def stat_file(self, entry: _SandboxEntry, path: str) -> FileInfo:
        status, resp = self._proxy(entry, "GET", f"/files/stat?path={_q(path)}")
        if status != 200:
            raise _agent_error("stat_file", status, resp)
        return _decode_file_info(json.loads(resp))

    def list_files(self, entry: _SandboxEntry, path: str) -> list[FileInfo]:
        status, resp = self._proxy(entry, "GET", f"/files/list?path={_q(path)}")
        if status != 200:
            raise _agent_error("list_files", status, resp)
        payload = json.loads(resp)
        entries = payload.get("entries", []) if isinstance(payload, dict) else payload
        return [_decode_file_info(e) for e in entries]

    def mkdir(self, entry: _SandboxEntry, path: str, parents: bool = True) -> None:
        status, resp = self._proxy(
            entry, "POST",
            f"/files/mkdir?path={_q(path)}&parents={'true' if parents else 'false'}",
        )
        if status not in (200, 201):
            raise _agent_error("mkdir", status, resp)

    def delete_file(self, entry: _SandboxEntry, path: str, recursive: bool = False) -> None:
        status, resp = self._proxy(
            entry, "DELETE",
            f"/files?path={_q(path)}&recursive={'true' if recursive else 'false'}",
        )
        if status not in (200, 204):
            raise _agent_error("delete_file", status, resp)

    # ── SupportsPty ──────────────────────────────────────────────────────

    def create_process(self, entry: _SandboxEntry, spec: ProcessSpec) -> ProcessHandle:
        body = json.dumps({
            "cmd": spec.cmd,
            "pty": spec.pty,
            "cols": spec.cols,
            "rows": spec.rows,
            "env": spec.env or {},
            "cwd": spec.cwd or "",
        }).encode()
        status, resp = self._proxy(entry, "POST", "/processes", body)
        if status != 201:
            raise _agent_error("create_process", status, resp)
        return ProcessHandle(pid=int(json.loads(resp)["pid"]))

    def list_processes(self, entry: _SandboxEntry) -> list[dict]:
        status, resp = self._proxy(entry, "GET", "/processes")
        if status != 200:
            raise _agent_error("list_processes", status, resp)
        payload = json.loads(resp)
        return payload.get("processes", payload) if isinstance(payload, dict) else payload

    def send_process_input(self, entry: _SandboxEntry, pid: int, data: bytes) -> None:
        status, resp = self._proxy(entry, "POST", f"/processes/{pid}/input", data, timeout=5)
        if status not in (200, 204):
            raise _agent_error("send_process_input", status, resp)

    def signal_process(self, entry: _SandboxEntry, pid: int, signal: int) -> None:
        body = json.dumps({"signal": signal}).encode()
        status, resp = self._proxy(entry, "POST", f"/processes/{pid}/signal", body, timeout=5)
        if status not in (200, 204):
            raise _agent_error("signal_process", status, resp)

    def resize_process(self, entry: _SandboxEntry, pid: int, cols: int, rows: int) -> None:
        body = json.dumps({"cols": cols, "rows": rows}).encode()
        status, resp = self._proxy(entry, "POST", f"/processes/{pid}/resize", body, timeout=5)
        if status not in (200, 204):
            raise _agent_error("resize_process", status, resp)

    async def attach_terminal(self, entry: _SandboxEntry, websocket) -> None:
        await websocket.accept()

        try:
            handle = await asyncio.get_event_loop().run_in_executor(
                None,
                self.create_process,
                entry,
                ProcessSpec(cmd=["/bin/sh", "-i"], pty=True, cols=120, rows=40),
            )
        except Exception:
            await websocket.close(code=1011, reason="Failed to create PTY process")
            return

        guest_pid = handle.pid
        loop = asyncio.get_event_loop()
        closed = False

        def _read_guest_ws():
            nonlocal closed
            try:
                orig_fd = _enter_netns(entry.ns_name)
                try:
                    guest_ws_url = f"ws://{entry.guest_ip}:{AGENT_PORT}/processes/{guest_pid}/output"
                    guest_ws = ws_sync.connect(guest_ws_url)
                finally:
                    _restore_netns(orig_fd)

                for message in guest_ws:
                    if closed:
                        break
                    try:
                        ev = json.loads(message)
                        if ev.get("type") in ("stdout", "stderr") and ev.get("data"):
                            raw = base64.b64decode(ev["data"])
                            asyncio.run_coroutine_threadsafe(websocket.send_bytes(raw), loop)
                        elif ev.get("type") == "exit":
                            break
                    except Exception:
                        pass
                guest_ws.close()
            except Exception:
                pass

        reader_thread = threading.Thread(target=_read_guest_ws, daemon=True)
        reader_thread.start()

        try:
            while True:
                data = await websocket.receive_bytes()
                await loop.run_in_executor(None, self.send_process_input, entry, guest_pid, data)
        except Exception:
            pass
        finally:
            closed = True
            try:
                await loop.run_in_executor(None, self.signal_process, entry, guest_pid, 9)
            except Exception:
                pass

    # ── Internal: HTTP transport into the guest agent ────────────────────

    def _proxy(
        self,
        entry: _SandboxEntry,
        method: str,
        path: str,
        body: bytes | None = None,
        timeout: float = 65,
    ) -> tuple[int, bytes]:
        url = f"{entry.agent_url}{path}"
        req = urllib.request.Request(url, data=body, method=method)
        if body is not None:
            req.add_header("Content-Type", "application/json")

        orig_fd = _enter_netns(entry.ns_name)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        finally:
            _restore_netns(orig_fd)

    @staticmethod
    def _cleanup_dead_row(row: dict) -> None:
        vm_id = row["vm_id"]
        pid = row["pid"]
        ns_name = row["ns_name"]
        chroot_base = row.get("chroot_base") or ""

        try:
            os.kill(pid, 9)
        except (ProcessLookupError, PermissionError):
            pass

        try:
            _delete_netns(ns_name)
        except Exception:
            pass

        if chroot_base:
            from flint.core._jailer import cleanup_jailer

            cleanup_jailer(chroot_base, vm_id)


# ── Module-private decoders ────────────────────────────────────────────────


def _q(s: str) -> str:
    return urllib.parse.quote(s, safe="/")


def _agent_error(op: str, status: int, body: bytes) -> RuntimeError:
    try:
        payload = json.loads(body) if body else {}
        detail = payload.get("error") or payload.get("detail") or body.decode(errors="replace")
    except Exception:
        detail = body.decode(errors="replace")
    return RuntimeError(f"{op} failed (status {status}): {detail}")


def _decode_exec_result(status: int, body: bytes) -> ExecResult:
    try:
        payload = json.loads(body) if body else {}
    except Exception:
        payload = {}
    if status >= 400 and "exit_code" not in payload:
        return ExecResult(stdout="", stderr=body.decode(errors="replace"), exit_code=-1)
    return ExecResult(
        stdout=payload.get("stdout", ""),
        stderr=payload.get("stderr", ""),
        exit_code=int(payload.get("exit_code", -1)),
    )


def _decode_file_info(payload: dict) -> FileInfo:
    return FileInfo(
        name=payload.get("name", ""),
        path=payload.get("path", ""),
        size=int(payload.get("size", 0)),
        is_dir=bool(payload.get("is_dir", False)),
        mode=str(payload.get("mode", "")),
        modified_at=float(payload.get("modified_at", 0.0)),
    )

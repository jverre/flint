from __future__ import annotations

import asyncio
import base64
import json
import os
import platform
import shutil
import threading
import urllib.request

import websockets.sync.client as ws_sync

from flint.core._boot import _RecoveredProcess, _boot_from_snapshot, _teardown_vm
from flint.core._firecracker import _fc_patch, _fc_put, _fc_request, _fc_status_ok, _wait_for_agent, _wait_for_api_socket
from flint.core._netns import _delete_netns, _ensure_bridge, _enter_netns, _restore_netns
from flint.core._pool import start_pool, stop_pool

from flint.core._snapshot import create_golden_snapshot, golden_snapshot_exists
from flint.core._template_build import build_template as _build_template
from flint.core._template_registry import (
    delete_template_artifact as _delete_template_artifact,
    get_template_dir,
    register_template_artifact,
    template_snapshot_exists,
)
from flint.core.config import (
    AGENT_PORT,
    DATA_DIR,
    DEFAULT_BASE_IMAGE,
    DEFAULT_TEMPLATE_ID,
    GOLDEN_DIR,
    GOLDEN_TAP,
    GUEST_IP,
    TEMPLATES_DIR,
    log,
)
from flint.core.types import _SandboxEntry, SandboxState

from .base import BackendBootResult, HostBackend


class LinuxFirecrackerBackend(HostBackend):
    kind = "linux-firecracker"

    def ensure_runtime_ready(self) -> None:
        _ensure_bridge()

    def ensure_default_template(self) -> None:
        if golden_snapshot_exists():
            register_template_artifact(
                DEFAULT_TEMPLATE_ID,
                "Default (Alpine)",
                self.kind,
                GOLDEN_DIR,
                status="ready",
            )
            return

        shutil.rmtree(GOLDEN_DIR, ignore_errors=True)

        from flint.core._oci_pull import pull_and_extract
        rootfs_path = os.path.join(GOLDEN_DIR, "rootfs.ext4")
        os.makedirs(GOLDEN_DIR, exist_ok=True)
        log.info("Pulling default base image from %s ...", DEFAULT_BASE_IMAGE)
        digest = pull_and_extract(
            DEFAULT_BASE_IMAGE,
            rootfs_path,
            size_mb=200,
            inject_flint=False,  # ghcr.io base image already has flintd
        )
        create_golden_snapshot(source_rootfs=rootfs_path, snapshot_dir=GOLDEN_DIR)
        register_template_artifact(
            DEFAULT_TEMPLATE_ID,
            "Default (Alpine)",
            self.kind,
            GOLDEN_DIR,
            status="ready",
            image_ref=DEFAULT_BASE_IMAGE,
            image_digest=digest,
        )

    def start_pool(self) -> None:
        start_pool()

    def stop_pool(self) -> None:
        stop_pool()

    def create(
        self,
        *,
        template_id: str,
        allow_internet_access: bool,
        use_pool: bool,
        use_pyroute2: bool,
    ) -> BackendBootResult:
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
                "veth_ip": boot.veth_ip,
            },
        )

    def kill(self, entry: _SandboxEntry) -> None:
        _teardown_vm(entry.process, entry.ns_name, entry.chroot_base, entry.vm_id)

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
        import shutil
        import subprocess

        sandbox_id = row["vm_id"]
        vm_dir = row["vm_dir"]
        ns_name = row["ns_name"]
        chroot_base = row.get("chroot_base") or ""

        spec = JailSpec(vm_id=sandbox_id, ns_name=ns_name)
        socket_path = spec.socket_path_on_host

        # The jailer creates several artifacts in the chroot on startup
        # (firecracker binary link, /dev/kvm, /dev/net/tun, /dev/urandom,
        # cgroup dirs). These conflict if the jailer is restarted with the
        # same --id. Remove everything except the files we need to keep.
        _keep = {"rootfs.ext4", "pause-vmstate", "pause-mem"}
        for entry in os.listdir(spec.chroot_root):
            if entry in _keep:
                continue
            path = os.path.join(spec.chroot_root, entry)
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                try:
                    os.unlink(path)
                except OSError:
                    pass
        # Also need to keep the symlink tree that maps the golden rootfs
        # path inside the chroot (e.g. microvms/.golden/rootfs.ext4 -> /rootfs.ext4).
        # _boot_from_snapshot recreates this, so we just ensure the parent exists.

        # Clean stale cgroup entries
        for cg_base in (
            f"/sys/fs/cgroup/firecracker/{sandbox_id}",
            f"/sys/fs/cgroup/cpu/firecracker/{sandbox_id}",
            f"/sys/fs/cgroup/memory/firecracker/{sandbox_id}",
            f"/sys/fs/cgroup/pids/firecracker/{sandbox_id}",
        ):
            if os.path.isdir(cg_base):
                shutil.rmtree(cg_base, ignore_errors=True)

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
                "network_overrides": [{"iface_id": "eth0", "host_dev_name": GOLDEN_TAP}],
            }
            resp = _fc_put(socket_path, "/snapshot/load", snapshot_body)
            if not _fc_status_ok(resp):
                raise RuntimeError(f"snapshot/load failed: {resp}")
            _fc_patch(socket_path, "/drives/rootfs", {"drive_id": "rootfs", "path_on_host": "rootfs.ext4"})
            _fc_patch(socket_path, "/vm", {"state": "Resumed"})
            agent_url = _wait_for_agent(ns_name)
        except Exception:
            # Capture firecracker/jailer log for debugging
            try:
                with open(log_path) as f:
                    fc_log = f.read()[-2000:]
                    log.error("Resume failed — firecracker log:\n%s", fc_log)
                    print(f"Resume failed — firecracker log:\n{fc_log}")
            except OSError:
                pass
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

    def proxy_guest_request(
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

    async def bridge_terminal(self, entry: _SandboxEntry, websocket) -> None:
        await websocket.accept()

        create_body = json.dumps({
            "cmd": ["/bin/sh", "-i"],
            "pty": True,
            "cols": 120,
            "rows": 40,
        }).encode()
        status, resp_body = await asyncio.get_event_loop().run_in_executor(
            None,
            self.proxy_guest_request,
            entry,
            "POST",
            "/processes",
            create_body,
            65,
        )
        if status != 201:
            await websocket.close(code=1011, reason="Failed to create PTY process")
            return

        proc_info = json.loads(resp_body)
        guest_pid = proc_info["pid"]
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
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.proxy_guest_request,
                    entry,
                    "POST",
                    f"/processes/{guest_pid}/input",
                    data,
                    5,
                )
        except Exception:
            pass
        finally:
            closed = True
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    self.proxy_guest_request,
                    entry,
                    "POST",
                    f"/processes/{guest_pid}/signal",
                    json.dumps({"signal": 9}).encode(),
                    2,
                )
            except Exception:
                pass

    def check_entry_alive(self, entry: _SandboxEntry) -> tuple[bool, str | None]:
        pid = entry.pid
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False, f"process {pid} not found"
        except PermissionError:
            pass
        return True, None

    @staticmethod
    def _probe_sandbox(row: dict) -> str:
        """Check if a sandbox process is still alive. Returns 'alive', 'paused', or 'dead'."""
        state = row.get("state", "")
        if state == "Paused":
            return "paused"
        pid = row.get("pid")
        if not pid:
            return "dead"
        try:
            os.kill(pid, 0)
            return "alive"
        except (OSError, ProcessLookupError):
            return "dead"

    def recover_row(self, row: dict):
        probe = self._probe_sandbox(row)
        if probe == "paused":
            return "paused", None
        if probe != "alive":
            self._cleanup_dead_row(row)
            return "dead", None

        vm_id = row["vm_id"]
        pid = row["pid"]
        ns_name = row["ns_name"]
        vm_dir = row["vm_dir"]

        try:
            agent_url = _wait_for_agent(ns_name, retries=50)
        except Exception:
            self._cleanup_dead_row(row)
            return "dead", None

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
        return "alive", entry

    def build_template(
        self, name: str, image_ref: str, rootfs_size_mb: int = 500, inject_flint: bool = True,
    ) -> dict:
        template_id = _build_template(name, image_ref, rootfs_size_mb=rootfs_size_mb, inject_flint=inject_flint)
        return {"template_id": template_id, "status": "building"}

    def delete_template_artifact(self, template_id: str, template: dict | None = None) -> None:
        if template is None:
            return
        _delete_template_artifact(template_id, self.kind)

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

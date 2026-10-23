"""Cloud-Hypervisor backend plugin.

Boots micro-VMs via the ``cloud-hypervisor`` binary using the REST API exposed
on a Unix domain socket. Reuses the same ``br-flint`` bridge + netns + TAP
infrastructure as the Firecracker backend; there is no jailer — isolation uses
CH's built-in ``--seccomp true`` and runs the binary inside a per-VM network
namespace.
"""

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

from flint.core import config as _cfg
from flint.core._ch_boot import (
    _RecoveredChProcess,
    _ch_ns_name,
    _ch_tap_name,
    ch_boot,
    ch_create_golden,
    ch_create_golden_snapshot,
    ch_golden_ready,
    ch_snapshot_ready,
    ch_tap_for_ns,
    ch_pause_vm,
    ch_resume_from_pause,
    ch_teardown,
)
from flint.core._ch_jail import CH_JAIL_DIR, ChJail
from flint.core._firecracker import _wait_for_agent
from flint.core._netns import _delete_netns, _enter_netns, _ensure_bridge, _restore_netns, ensure_host_firewall, reserve_existing_veth_ips
from flint.core._netns_pool import start_netns_pool, stop_netns_pool
from flint.core._pool import start_pool, stop_pool
from flint.core.config import (
    AGENT_PORT,
    CH_GOLDEN_DIR,
    DEFAULT_TEMPLATE_ID,
    GUEST_IP,
    log,
)
from flint.core.types import _SandboxEntry, SandboxState

from .base import BackendBootResult, HostBackend, row_backend_metadata


class LinuxCloudHypervisorBackend(HostBackend):
    name = "cloud-hypervisor"
    kind = "linux-cloud-hypervisor"
    display_name = "Cloud-Hypervisor (Linux)"
    supported_platforms = ("linux",)

    def preflight(self) -> list[str]:
        problems = super().preflight()
        if problems:
            return problems
        # Read CH_BINARY from the live config module so env-var overrides
        # (e.g. in tests that reload config) are honored.
        binary = _cfg.CH_BINARY
        if not os.path.exists(binary) and not shutil.which("cloud-hypervisor"):
            problems.append(
                f"cloud-hypervisor binary not found at {binary} or on PATH"
            )
        if not os.path.exists(_cfg.KERNEL_PATH):
            problems.append(f"kernel image not found at {_cfg.KERNEL_PATH}")
        if not os.path.exists(_cfg.SOURCE_ROOTFS):
            problems.append(f"source rootfs not found at {_cfg.SOURCE_ROOTFS}")
        for tool in ("setpriv", "chroot"):
            if not shutil.which(tool):
                problems.append(f"{tool} not found on PATH (needed to jail cloud-hypervisor)")
        return problems

    def template_artifact_valid(self, template_dir: str) -> bool:
        # A rootfs is enough: without a golden snapshot, create() cold-boots.
        # Per-sandbox pause snapshots live under each sandbox's vm_dir.
        return os.path.exists(os.path.join(template_dir, "rootfs.ext4"))

    def install_dependencies(self, **kwargs) -> None:
        from flint.core._install import KERNEL_RELEASE, _detect_arch, _install_kernel, install_cloud_hypervisor

        install_cloud_hypervisor(
            version=kwargs.get("version", "latest"),
            install_dir=kwargs.get("install_dir", "/usr/local/bin"),
        )
        if not kwargs.get("skip_kernel", False):
            _install_kernel(
                kwargs.get("kernel_release") or KERNEL_RELEASE,
                _detect_arch(),
                kwargs.get("kernel_dir", "/root/firecracker-vm"),
            )

    def ensure_runtime_ready(self) -> None:
        _ensure_bridge()
        ensure_host_firewall()
        reserve_existing_veth_ips()

    def ensure_default_template(self) -> None:
        from flint.core._template_registry import register_template_artifact

        os.makedirs(CH_GOLDEN_DIR, exist_ok=True)
        status = "ready"
        try:
            ch_create_golden(dest_dir=CH_GOLDEN_DIR, source_rootfs=_cfg.SOURCE_ROOTFS)
        except Exception as exc:
            if ch_golden_ready(CH_GOLDEN_DIR):
                # Sandboxes still work, they just cold-boot instead of restoring.
                log.warning("ch golden snapshot not created, falling back to fresh boot: %s", exc)
            else:
                log.warning("ch golden template not installed: %s", exc)
                status = "pending"

        register_template_artifact(
            DEFAULT_TEMPLATE_ID,
            "Default (Alpine)",
            self.kind,
            CH_GOLDEN_DIR,
            status=status,
        )
        log.info(
            "cloud-hypervisor default template registered (status=%s, dir=%s)",
            status,
            CH_GOLDEN_DIR,
        )

    def start_pool(self) -> None:
        start_pool(_cfg.CH_POOL_DIR, self._pool_sources)
        start_netns_pool("ch-", ch_tap_for_ns)

    def stop_pool(self) -> None:
        stop_pool()
        stop_netns_pool()

    @staticmethod
    def _pool_sources(template_id: str) -> dict[str, str] | None:
        from flint.core._template_registry import get_template_dir

        if template_id == DEFAULT_TEMPLATE_ID:
            template_dir = CH_GOLDEN_DIR
        else:
            try:
                template_dir = get_template_dir(template_id, backend_kind=LinuxCloudHypervisorBackend.kind)
            except KeyError:
                return None
        if not ch_snapshot_ready(template_dir):
            return None
        return {"rootfs.ext4": os.path.join(template_dir, "snapshot", "rootfs.ext4")}

    def create(
        self,
        *,
        template_id: str,
        allow_internet_access: bool,
        use_pool: bool,
        use_pyroute2: bool,
        vm_id: str | None = None,
    ) -> BackendBootResult:
        if template_id == DEFAULT_TEMPLATE_ID and not ch_golden_ready(CH_GOLDEN_DIR):
            raise RuntimeError(
                f"cloud-hypervisor golden rootfs not found in {CH_GOLDEN_DIR}"
            )

        boot = ch_boot(
            template_id=template_id,
            allow_internet_access=allow_internet_access,
            use_pool=use_pool,
            use_pyroute2=use_pyroute2,
            vm_id=vm_id,
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
                "api_socket": boot.socket_path,
                "ns_name": boot.ns_name,
                "tap_name": boot.tap_name,
                "veth_ip": boot.veth_ip,
            },
        )

    def vm_resources(self) -> tuple[int, int]:
        from flint.core.config import CH_CPU_COUNT, CH_MEMORY_BYTES
        return CH_CPU_COUNT, CH_MEMORY_BYTES >> 20

    def kill(self, entry: _SandboxEntry) -> None:
        ns_name = entry.ns_name or (entry.backend_metadata or {}).get("ns_name") or _ch_ns_name(entry.vm_id)
        socket_path = entry.socket_path or (entry.backend_metadata or {}).get("api_socket") or ""
        ch_teardown(entry.process, ns_name, entry.vm_id, socket_path=socket_path)

    def pause(self, entry: _SandboxEntry, state_store) -> None:
        pause_dir = ch_pause_vm(entry.vm_id)

        # Stop the CH process — vm.restore on resume needs a fresh cloud-hypervisor
        # instance pointed at the same api-socket path.
        if entry.process:
            try:
                entry.process.kill()
                entry.process.wait(timeout=2)
            except Exception:
                pass

        entry.state = SandboxState.PAUSED
        entry.agent_healthy = False
        entry.process = None
        entry.pause_state_ref = pause_dir
        meta = dict(entry.backend_metadata or {})
        meta["pause_dir"] = pause_dir
        entry.backend_metadata = meta

        if state_store:
            state_store.transition_state(entry.vm_id, SandboxState.PAUSED)
            state_store.set_pause_snapshot(entry.vm_id, pause_dir)
            state_store.update_sandbox(entry.vm_id, pause_state_ref=pause_dir)

    def resume(self, row: dict) -> BackendBootResult:
        vm_id = row["vm_id"]
        ns_name = row.get("ns_name") or _ch_ns_name(vm_id)
        meta = row_backend_metadata(row)
        tap_name = meta.get("tap_name") or _ch_tap_name(vm_id)

        boot = ch_resume_from_pause(
            vm_id,
            ns_name=ns_name,
            tap_name=tap_name,
            veth_ip=meta.get("veth_ip", ""),
        )
        vm_dir = boot.vm_dir
        pause_dir = os.path.join(vm_dir, "pause")

        return BackendBootResult(
            process=boot.process,
            pid=boot.process.pid,
            vm_dir=vm_dir,
            socket_path=boot.socket_path,
            ns_name=ns_name,
            guest_ip=GUEST_IP,
            agent_url=boot.agent_url,
            chroot_base=boot.chroot_base,
            backend_vm_ref=vm_id,
            runtime_dir=vm_dir,
            guest_arch=platform.machine().lower(),
            transport_ref=f"netns:{ns_name}",
            timings=dict(boot.timings),
            t_total=boot.t_total,
            backend_metadata={
                "api_socket": boot.socket_path,
                "ns_name": ns_name,
                "tap_name": tap_name,
                "pause_dir": pause_dir,
                "veth_ip": boot.veth_ip,
            },
        )

    # ── Cluster hooks ──────────────────────────────────────────────────────

    _PAUSE_FILES = ("config.json", "state.json", "memory-ranges")

    def pause_files(self, row: dict) -> tuple[str, dict[str, str]]:
        files = {"rootfs.ext4": "rootfs.ext4"}
        files.update({f"pause/{n}": f"pause/{n}" for n in self._PAUSE_FILES})
        return ChJail(row["vm_id"]).root, files

    def checkpoint(self, entry: _SandboxEntry, snapshot):
        from flint.core import _cloud_hypervisor as _ch

        jail = ChJail(entry.vm_id)
        staging = jail.host_path("/ckpt")
        shutil.rmtree(staging, ignore_errors=True)
        os.makedirs(staging)
        jail.give("/ckpt")
        _ch.vm_pause(jail.socket_path)
        try:
            _ch.vm_snapshot(jail.socket_path, "file:///ckpt")
            files = {"rootfs.ext4": "rootfs.ext4"}
            files.update({f"pause/{n}": f"ckpt/{n}" for n in self._PAUSE_FILES})
            return snapshot(jail.root, files)
        finally:
            try:
                _ch.vm_resume(jail.socket_path)
            finally:
                from flint.core._wipe import wipe_tree
                wipe_tree(staging, entry.vm_id)  # a copy of the VM's memory

    def import_snapshot(self, vm_id: str, src_dir: str, *, allow_internet_access: bool, use_pool: bool = True) -> BackendBootResult:
        from flint.core._ch_jail import JAIL_ROOTFS
        from flint.core._netns_pool import acquire_netns

        jail = ChJail(vm_id)
        jail.cleanup()  # a stale jail from an earlier life of this ID
        jail.prepare()
        net = None
        try:
            os.rename(os.path.join(src_dir, "rootfs.ext4"), jail.host_path(JAIL_ROOTFS))
            jail.give(JAIL_ROOTFS)
            os.mkdir(jail.host_path("/pause"))
            for n in self._PAUSE_FILES:
                os.rename(os.path.join(src_dir, "pause", n), jail.host_path(f"/pause/{n}"))
                jail.give(f"/pause/{n}")
            jail.give("/pause")
            net = acquire_netns("ch-", ch_tap_for_ns, allow_internet_access, use_pool=use_pool)
            boot = ch_resume_from_pause(
                vm_id, ns_name=net.ns_name, tap_name=net.tap_name,
                allow_internet_access=allow_internet_access, veth_ip=net.veth_ip,
            )
        except Exception:
            if net is not None:
                _delete_netns(net.ns_name)
            jail.cleanup()
            raise
        return BackendBootResult(
            process=boot.process,
            pid=boot.process.pid,
            vm_dir=boot.vm_dir,
            socket_path=boot.socket_path,
            ns_name=net.ns_name,
            guest_ip=GUEST_IP,
            agent_url=boot.agent_url,
            chroot_base=boot.chroot_base,
            backend_vm_ref=vm_id,
            runtime_dir=boot.vm_dir,
            guest_arch=platform.machine().lower(),
            transport_ref=f"netns:{net.ns_name}",
            timings=dict(boot.timings),
            t_total=boot.t_total,
            backend_metadata={
                "api_socket": boot.socket_path,
                "ns_name": net.ns_name,
                "tap_name": net.tap_name,
                "pause_dir": os.path.join(boot.vm_dir, "pause"),
                "veth_ip": net.veth_ip,
            },
        )

    def template_files(self, template_dir: str) -> list[str]:
        return ["rootfs.ext4"] + [f"snapshot/{n}" for n in (*self._PAUSE_FILES, "rootfs.ext4")]

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

        ns_name = entry.ns_name or (entry.backend_metadata or {}).get("ns_name")
        orig_fd = _enter_netns(ns_name)
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

        ns_name = entry.ns_name or (entry.backend_metadata or {}).get("ns_name")

        def _read_guest_ws():
            nonlocal closed
            try:
                orig_fd = _enter_netns(ns_name)
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
        pid = getattr(entry, "pid", 0)
        if not pid:
            return False, "no pid recorded"
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False, f"process {pid} not found"
        except PermissionError:
            pass
        return True, None

    @staticmethod
    def _probe_sandbox(row: dict) -> str:
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
        ns_name = row.get("ns_name") or _ch_ns_name(vm_id)
        vm_dir = row["vm_dir"]

        try:
            agent_url = _wait_for_agent(ns_name, retries=50)
        except Exception:
            self._cleanup_dead_row(row)
            return "dead", None

        meta = row_backend_metadata(row)

        entry = _SandboxEntry(
            vm_id=vm_id,
            process=_RecoveredChProcess(pid),
            pid=pid,
            vm_dir=vm_dir,
            socket_path=row.get("socket_path") or meta.get("api_socket") or os.path.join(vm_dir, "ch.sock"),
            ns_name=ns_name,
            guest_ip=GUEST_IP,
            agent_url=agent_url,
            agent_healthy=True,
            state=SandboxState.RUNNING,
            template_id=row.get("template_id", DEFAULT_TEMPLATE_ID),
            chroot_base=row.get("chroot_base") or "",
            backend_kind=row.get("backend_kind") or self.kind,
            backend_vm_ref=vm_id,
            runtime_dir=vm_dir,
            guest_arch=platform.machine().lower(),
            transport_ref=f"netns:{ns_name}",
            backend_metadata=dict(meta),
        )
        return "alive", entry

    def build_template(self, name: str, dockerfile: str, rootfs_size_mb: int = 500) -> dict:
        """Build a CH template: Docker build -> rootfs extraction -> golden snapshot -> register."""
        from flint.core._template_build import (
            _build_flintd,
            _docker_build,
            _extract_rootfs,
            _find_init_net_sh,
            _slugify,
        )
        from flint.core._template_registry import (
            register_template_artifact,
            update_template_artifact_status,
        )
        from flint.core.config import TEMPLATES_DIR

        template_id = _slugify(name)
        template_dir = f"{TEMPLATES_DIR}/{template_id}"
        os.makedirs(template_dir, exist_ok=True)

        register_template_artifact(
            template_id,
            name,
            self.kind,
            template_dir,
            status="building",
            rootfs_size_mb=rootfs_size_mb,
        )

        context_dir = f"/tmp/flint-ch-build-{template_id}"
        os.makedirs(context_dir, exist_ok=True)

        try:
            _build_flintd(os.path.join(context_dir, "flintd"))
            init_net = _find_init_net_sh()
            shutil.copy2(init_net, os.path.join(context_dir, "init-net.sh"))

            with open(f"{template_dir}/Dockerfile", "w") as f:
                f.write(dockerfile)

            image_tag = _docker_build(template_id, dockerfile, context_dir)

            rootfs_path = f"{template_dir}/rootfs.ext4"
            _extract_rootfs(image_tag, rootfs_path, rootfs_size_mb)
            ch_create_golden_snapshot(template_dir)

            update_template_artifact_status(template_id, self.kind, "ready")

            import subprocess

            subprocess.run(["docker", "rmi", image_tag], capture_output=True)
            shutil.rmtree(context_dir, ignore_errors=True)

            log.info("CH template %s built successfully", template_id)
            return {"template_id": template_id, "status": "ready"}
        except Exception:
            update_template_artifact_status(template_id, self.kind, "failed")
            shutil.rmtree(context_dir, ignore_errors=True)
            raise

    def delete_template_artifact(self, template_id: str, template: dict | None = None) -> None:
        from flint.core._template_registry import delete_template_artifact as _delete_template_artifact

        _delete_template_artifact(template_id, self.kind)

    def discard_paused(self, row: dict) -> None:
        # Its VMM exited at pause; the recorded pid may belong to something else now.
        self._cleanup_dead_row({**row, "pid": 0})

    @staticmethod
    def _cleanup_dead_row(row: dict) -> None:
        vm_id = row["vm_id"]
        pid = row.get("pid", 0)
        ns_name = row.get("ns_name") or _ch_ns_name(vm_id)
        vm_dir = row.get("vm_dir") or ""

        if pid:
            try:
                os.kill(pid, 9)
            except (ProcessLookupError, PermissionError):
                pass

        try:
            _delete_netns(ns_name)
        except Exception:
            pass

        ChJail(vm_id).cleanup()
        # Sandboxes created before CH was jailed kept their files in the data dir.
        if vm_dir and not vm_dir.startswith(CH_JAIL_DIR):
            from flint.core._wipe import wipe_tree
            wipe_tree(vm_dir, vm_id)


from .registry import register as _register

_register(LinuxCloudHypervisorBackend)

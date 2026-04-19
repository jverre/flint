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
    ch_boot_fresh,
    ch_create_golden,
    ch_golden_ready,
    ch_pause_vm,
    ch_resume_from_pause,
    ch_teardown,
)
from flint.core._firecracker import _wait_for_agent
from flint.core._netns import _delete_netns, _enter_netns, _ensure_bridge, _restore_netns
from flint.core.config import (
    AGENT_PORT,
    CH_GOLDEN_DIR,
    DEFAULT_TEMPLATE_ID,
    GUEST_IP,
    log,
)
from flint.core.types import _SandboxEntry, SandboxState

from .base import BackendBootResult, HostBackend


def _row_meta(row: dict) -> dict:
    """Return the per-sandbox backend_metadata dict from a state-store row.

    Rows can carry it as either a pre-parsed dict (``backend_metadata``) or
    the raw ``backend_meta_json`` TEXT column. Accept either.
    """
    meta = row.get("backend_metadata")
    if isinstance(meta, dict):
        return meta
    raw = row.get("backend_meta_json")
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return {}


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
        return problems

    def template_artifact_valid(self, template_dir: str) -> bool:
        # CH uses fresh boot per sandbox, so the default template only needs a
        # rootfs. Per-sandbox pause snapshots live under each sandbox's vm_dir.
        return os.path.exists(os.path.join(template_dir, "rootfs.ext4"))

    def install_dependencies(self, **kwargs) -> None:
        install_dir = kwargs.get("install_dir", "/usr/local/bin")
        version = kwargs.get("version", "latest")
        _install_ch_binary(install_dir=install_dir, version=version)

    def ensure_runtime_ready(self) -> None:
        _ensure_bridge()

    def ensure_default_template(self) -> None:
        from flint.core._template_registry import register_template_artifact

        os.makedirs(CH_GOLDEN_DIR, exist_ok=True)
        status = "ready"
        try:
            ch_create_golden(dest_dir=CH_GOLDEN_DIR, source_rootfs=_cfg.SOURCE_ROOTFS)
        except Exception as exc:
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
        # CH currently doesn't use a pre-warmed pool — fresh boot is fast
        # enough for the test matrix, and the pool infra is keyed by
        # firecracker-specific chroot staging.
        pass

    def stop_pool(self) -> None:
        pass

    def create(
        self,
        *,
        template_id: str,
        allow_internet_access: bool,
        use_pool: bool,
        use_pyroute2: bool,
    ) -> BackendBootResult:
        if template_id == DEFAULT_TEMPLATE_ID and not ch_golden_ready(CH_GOLDEN_DIR):
            raise RuntimeError(
                f"cloud-hypervisor golden rootfs not found in {CH_GOLDEN_DIR}"
            )

        boot = ch_boot_fresh(
            template_id=template_id,
            allow_internet_access=allow_internet_access,
            use_pyroute2=use_pyroute2,
        )

        return BackendBootResult(
            process=boot.process,
            pid=boot.process.pid,
            vm_dir=boot.vm_dir,
            socket_path=boot.socket_path,
            ns_name=boot.ns_name,
            guest_ip=GUEST_IP,
            agent_url=boot.agent_url,
            chroot_base="",
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

    def kill(self, entry: _SandboxEntry) -> None:
        ns_name = entry.ns_name or (entry.backend_metadata or {}).get("ns_name") or _ch_ns_name(entry.vm_id)
        socket_path = entry.socket_path or (entry.backend_metadata or {}).get("api_socket") or ""
        ch_teardown(entry.process, ns_name, entry.vm_dir, socket_path=socket_path)

    def pause(self, entry: _SandboxEntry, state_store) -> None:
        socket_path = entry.socket_path or (entry.backend_metadata or {}).get("api_socket")
        if not socket_path:
            raise RuntimeError("cloud-hypervisor pause: missing api socket path")

        pause_dir = ch_pause_vm(socket_path, entry.vm_dir)

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
        vm_dir = row["vm_dir"]
        ns_name = row.get("ns_name") or _ch_ns_name(vm_id)
        meta = _row_meta(row)
        tap_name = meta.get("tap_name") or _ch_tap_name(vm_id)
        pause_dir = row.get("pause_state_ref") or meta.get("pause_dir") or os.path.join(vm_dir, "pause")

        boot = ch_resume_from_pause(
            vm_id=vm_id,
            vm_dir=vm_dir,
            pause_dir=pause_dir,
            ns_name=ns_name,
            tap_name=tap_name,
        )

        return BackendBootResult(
            process=boot.process,
            pid=boot.process.pid,
            vm_dir=vm_dir,
            socket_path=boot.socket_path,
            ns_name=ns_name,
            guest_ip=GUEST_IP,
            agent_url=boot.agent_url,
            chroot_base="",
            backend_vm_ref=vm_id,
            runtime_dir=vm_dir,
            guest_arch=row.get("guest_arch") or platform.machine().lower(),
            transport_ref=row.get("transport_ref") or f"netns:{ns_name}",
            timings=dict(boot.timings),
            t_total=boot.t_total,
            backend_metadata={
                "api_socket": boot.socket_path,
                "ns_name": ns_name,
                "tap_name": tap_name,
                "pause_dir": pause_dir,
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

        meta = _row_meta(row)

        entry = _SandboxEntry(
            vm_id=vm_id,
            process=_RecoveredChProcess(pid),
            pid=pid,
            vm_dir=vm_dir,
            socket_path=row.get("socket_path") or meta.get("api_socket") or os.path.join(vm_dir, "ch.sock"),
            ns_name=ns_name,
            guest_ip=row.get("guest_ip") or GUEST_IP,
            agent_url=agent_url,
            agent_healthy=True,
            state=SandboxState.RUNNING,
            template_id=row.get("template_id", DEFAULT_TEMPLATE_ID),
            chroot_base="",
            backend_kind=row.get("backend_kind") or self.kind,
            backend_vm_ref=row.get("backend_vm_ref") or vm_id,
            runtime_dir=row.get("runtime_dir") or vm_dir,
            guest_arch=row.get("guest_arch") or "linux",
            transport_ref=row.get("transport_ref") or f"netns:{ns_name}",
            backend_metadata=dict(meta),
        )
        return "alive", entry

    def build_template(self, name: str, dockerfile: str, rootfs_size_mb: int = 500) -> dict:
        """Build a CH template: Docker build -> rootfs extraction -> register.

        CH doesn't need a pre-baked snapshot (we fresh-boot per sandbox) so
        this skips the FC ``create_golden_snapshot`` step.
        """
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

        if vm_dir:
            shutil.rmtree(vm_dir, ignore_errors=True)


def _install_ch_binary(*, install_dir: str, version: str) -> None:
    """Download the cloud-hypervisor binary release into ``install_dir``.

    Uses the GitHub release asset for the current host architecture. Raises on
    failure; caller can catch and report.
    """
    import subprocess
    import urllib.request

    arch = platform.machine().lower()
    if arch in ("x86_64", "amd64"):
        asset_arch = "x86_64"
    elif arch in ("aarch64", "arm64"):
        asset_arch = "aarch64"
    else:
        raise RuntimeError(f"cloud-hypervisor has no release asset for {arch!r}")

    if version == "latest":
        with urllib.request.urlopen(
            "https://api.github.com/repos/cloud-hypervisor/cloud-hypervisor/releases/latest",
            timeout=30,
        ) as resp:
            import json as _json
            tag = _json.load(resp)["tag_name"]
    else:
        tag = version if version.startswith("v") else f"v{version}"

    url = (
        f"https://github.com/cloud-hypervisor/cloud-hypervisor/releases/"
        f"download/{tag}/cloud-hypervisor-static"
        f"{'' if asset_arch == 'x86_64' else '-aarch64'}"
    )
    os.makedirs(install_dir, exist_ok=True)
    target = os.path.join(install_dir, "cloud-hypervisor")
    log.info("Downloading cloud-hypervisor %s from %s -> %s", tag, url, target)
    urllib.request.urlretrieve(url, target)
    os.chmod(target, 0o755)
    subprocess.run([target, "--version"], check=True, capture_output=True)


from .registry import register as _register

_register(LinuxCloudHypervisorBackend)

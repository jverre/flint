from __future__ import annotations

import asyncio
import base64
import json
import os
import queue
import re
import shutil
import threading
import time
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

import websockets.sync.client as ws_sync

from flint.core._template_registry import register_template_artifact, update_template_artifact_status
from flint.core.config import (
    AGENT_PORT,
    DATA_DIR,
    DEFAULT_TEMPLATE_ID,
    TEMPLATES_DIR,
    VZ_BOOT_ARGS,
    VZ_CPU_COUNT,
    VZ_INITRD_PATH,
    VZ_KERNEL_PATH,
    VZ_MEMORY_BYTES,
    VZ_READY_TIMEOUT,
    VZ_ROOTFS_PATH,
)
from flint.core.types import SandboxState

from .base import ALIVE, DEAD, PAUSED, Backend, BackendBootResult
from .capabilities import (
    ExecRequest,
    ExecResult,
    FileInfo,
    ProcessHandle,
    ProcessSpec,
)


@dataclass
class _MacVMHandle:
    vm: Any
    runtime_dir: str
    disk_path: str
    guest_ip: str
    console_log_path: str
    reader_fd: int
    stdin_write_fd: int
    read_thread: threading.Thread | None = None
    stop_reading: threading.Event = field(default_factory=threading.Event)
    machine_id_path: str = ""
    boot_config_path: str = ""
    state_path: str = ""
    process_id: int = field(default_factory=os.getpid)
    # The VZVirtualMachineConfiguration used to boot this VM. Needed for
    # save/restore validation since VZVirtualMachine doesn't expose it back.
    config: Any = None
    # Keep references to ObjC objects so PyObjC doesn't GC them (closing pipe fds).
    _objc_refs: list[Any] = field(default_factory=list)

    @property
    def pid(self) -> int:
        return self.process_id

    def close(self) -> None:
        self.stop_reading.set()
        for fd in (self.reader_fd, self.stdin_write_fd):
            try:
                os.close(fd)
            except OSError:
                pass


class _BackendThread:
    """Run VZ operations on the main thread with an NSRunLoop.

    Apple's Virtualization.framework requires:
    1. ``VZVirtualMachine`` API calls on the main dispatch queue.
    2. The main thread's NSRunLoop to be active for the VM to execute.

    Operations are submitted via :meth:`call` (from any thread) and executed
    cooperatively on the main thread by :meth:`run_loop`.
    """

    def __init__(self) -> None:
        self._queue: queue.Queue[tuple[Callable[[], Any], queue.Queue[tuple[bool, Any]]]] = queue.Queue()
        self._started = threading.Event()

    def run_loop(self) -> None:
        """Block the calling (main) thread, pumping the RunLoop and processing VZ ops."""
        from Foundation import NSRunLoop, NSDate

        self._started.set()
        while True:
            NSRunLoop.currentRunLoop().runUntilDate_(
                NSDate.dateWithTimeIntervalSinceNow_(0.05)
            )
            try:
                fn, result_q = self._queue.get_nowait()
                try:
                    result_q.put((True, fn()))
                except Exception as exc:
                    result_q.put((False, exc))
            except queue.Empty:
                pass

    def call(self, fn: Callable[[], Any]) -> Any:
        """Submit *fn* for execution on the main thread and block until done."""
        result_q: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)
        self._queue.put((fn, result_q))
        ok, value = result_q.get()
        if ok:
            return value
        raise value


class MacOSVirtualizationBackend(Backend):
    """Apple Virtualization.framework backend for Apple Silicon hosts.

    Provides shell, files, PTY, pause/resume by proxying to the in-guest
    flintd HTTP agent over the VZ NAT network.
    """

    kind = "macos-vz-arm64"

    def __init__(self) -> None:
        self._runtime = _BackendThread()
        self._handles: dict[str, _MacVMHandle] = {}

    def ensure_runtime_ready(self) -> None:
        try:
            __import__("objc")
            __import__("Virtualization")
            __import__("Foundation")
        except Exception as exc:
            raise RuntimeError(
                "macOS backend selected but PyObjC Virtualization bindings are not installed. "
                "Install pyobjc-core, pyobjc-framework-Cocoa, and pyobjc-framework-Virtualization."
            ) from exc

    def ensure_default_template(self) -> None:
        template_dir = os.path.join(TEMPLATES_DIR, DEFAULT_TEMPLATE_ID, self.kind)
        os.makedirs(template_dir, exist_ok=True)
        status = "ready" if self._default_assets_ready() else "pending"
        register_template_artifact(
            DEFAULT_TEMPLATE_ID,
            "Default (macOS arm64)",
            self.kind,
            template_dir,
            status=status,
        )
        if status == "ready":
            self._write_artifact_config(
                template_dir,
                {
                    "kernel_path": VZ_KERNEL_PATH,
                    "rootfs_path": VZ_ROOTFS_PATH,
                    "initrd_path": VZ_INITRD_PATH,
                    "boot_args": VZ_BOOT_ARGS,
                    "cpu_count": VZ_CPU_COUNT,
                    "memory_bytes": VZ_MEMORY_BYTES,
                },
            )

    def create(
        self,
        *,
        template_id: str,
        options=None,
    ) -> BackendBootResult:
        # macOS VZ ignores all VM-flavor options today (no internet toggle, no pool).
        _ = options
        if template_id != DEFAULT_TEMPLATE_ID:
            raise NotImplementedError("Custom macOS templates are not implemented yet.")

        artifact = self._load_artifact_config(os.path.join(TEMPLATES_DIR, template_id, self.kind))
        if not artifact:
            raise RuntimeError(
                "macOS backend requires a prepared arm64 guest artifact. "
                "Set FLINT_VZ_KERNEL_PATH and FLINT_VZ_ROOTFS_PATH to compatible files."
            )

        for key in ("kernel_path", "rootfs_path"):
            if not os.path.exists(artifact[key]):
                raise RuntimeError(f"Configured macOS backend artifact is missing: {artifact[key]}")

        vm_id = str(uuid.uuid4())
        runtime_dir = os.path.join(DATA_DIR, vm_id)
        os.makedirs(runtime_dir, exist_ok=True)
        disk_path = os.path.join(runtime_dir, "rootfs.img")
        shutil.copy2(artifact["rootfs_path"], disk_path)

        machine_id_path = os.path.join(runtime_dir, "machine-id.bin")
        boot_config_path = os.path.join(runtime_dir, "boot.json")
        console_log_path = os.path.join(runtime_dir, "console.log")
        state_path = os.path.join(runtime_dir, "pause.state")

        with open(boot_config_path, "w") as f:
            json.dump(artifact, f)

        t0 = time.monotonic()
        handle = self._runtime.call(
            lambda: self._start_vm(
                vm_id=vm_id,
                runtime_dir=runtime_dir,
                disk_path=disk_path,
                machine_id_path=machine_id_path,
                boot_config=artifact,
                console_log_path=console_log_path,
                state_path=state_path,
                resume_state_path=None,
            )
        )
        self._handles[vm_id] = handle

        total_ms = (time.monotonic() - t0) * 1000
        return BackendBootResult(
            process=handle,
            pid=handle.process_id,
            vm_dir=runtime_dir,
            socket_path="",
            ns_name="",
            guest_ip=handle.guest_ip,
            agent_url=f"http://{handle.guest_ip}:{AGENT_PORT}",
            chroot_base="",
            backend_vm_ref=vm_id,
            runtime_dir=runtime_dir,
            guest_arch="arm64",
            transport_ref=f"tcp:{handle.guest_ip}:{AGENT_PORT}",
            timings={
                "artifact_prepare_ms": total_ms,
            },
            t_total=t0,
            backend_metadata={
                "boot_config_path": boot_config_path,
                "machine_id_path": machine_id_path,
                "console_log_path": console_log_path,
                "guest_ip": handle.guest_ip,
                "state_path": state_path,
            },
        )

    def kill(self, entry) -> None:
        handle = self._coerce_handle(entry)
        if handle is None:
            return
        self._runtime.call(lambda: self._stop_vm(handle))
        handle.close()
        self._handles.pop(entry.vm_id, None)
        shutil.rmtree(handle.runtime_dir, ignore_errors=True)

    def pause(self, entry, state_store) -> None:
        handle = self._coerce_handle(entry)
        if handle is None:
            raise RuntimeError("macOS VM handle not found")

        # VZ requires the VM be paused (suspended) before its state can be saved.
        self._runtime.call(lambda: self._suspend_vm(handle))
        self._runtime.call(lambda: self._save_vm_state(handle))
        self._runtime.call(lambda: self._stop_vm(handle))
        handle.close()
        self._handles.pop(entry.vm_id, None)

        entry.state = SandboxState.PAUSED
        entry.agent_healthy = False
        entry.process = None
        entry.pause_state_ref = handle.state_path

        if state_store:
            state_store.transition_state(entry.vm_id, SandboxState.PAUSED)
            state_store.set_pause_snapshot(entry.vm_id, handle.state_path)

    def resume(self, row: dict) -> BackendBootResult:
        runtime_dir = row["vm_dir"]
        boot_config_path = row.get("backend_meta_json")
        if boot_config_path:
            try:
                meta = json.loads(boot_config_path)
            except Exception:
                meta = {}
        else:
            meta = {}
        boot_path = meta.get("boot_config_path") or os.path.join(runtime_dir, "boot.json")
        machine_id_path = meta.get("machine_id_path") or os.path.join(runtime_dir, "machine-id.bin")
        console_log_path = meta.get("console_log_path") or os.path.join(runtime_dir, "console.log")
        state_path = row.get("pause_state_ref") or meta.get("state_path") or os.path.join(runtime_dir, "pause.state")
        disk_path = os.path.join(runtime_dir, "rootfs.img")
        boot_config = self._read_json(boot_path)
        if not boot_config:
            raise RuntimeError("Missing macOS boot metadata for resume")
        if not os.path.exists(state_path):
            raise RuntimeError(f"Pause state missing: {state_path}")

        handle = self._runtime.call(
            lambda: self._start_vm(
                vm_id=row["vm_id"],
                runtime_dir=runtime_dir,
                disk_path=disk_path,
                machine_id_path=machine_id_path,
                boot_config=boot_config,
                console_log_path=console_log_path,
                state_path=state_path,
                resume_state_path=state_path,
            )
        )
        self._handles[row["vm_id"]] = handle
        return BackendBootResult(
            process=handle,
            pid=handle.process_id,
            vm_dir=runtime_dir,
            guest_ip=handle.guest_ip,
            agent_url=f"http://{handle.guest_ip}:{AGENT_PORT}",
            backend_vm_ref=row["vm_id"],
            runtime_dir=runtime_dir,
            guest_arch="arm64",
            transport_ref=f"tcp:{handle.guest_ip}:{AGENT_PORT}",
            backend_metadata={
                "boot_config_path": boot_path,
                "machine_id_path": machine_id_path,
                "console_log_path": console_log_path,
                "guest_ip": handle.guest_ip,
                "state_path": state_path,
            },
        )

    # ── SupportsShell ────────────────────────────────────────────────────

    def exec(self, entry, request: ExecRequest) -> ExecResult:
        body = json.dumps({
            "cmd": request.cmd,
            "env": request.env or {},
            "cwd": request.cwd or "",
            "timeout": int(request.timeout),
        }).encode()
        status, resp = self._proxy(entry, "POST", "/exec", body, timeout=request.timeout + 5)
        return _decode_exec_result(status, resp)

    # ── SupportsFiles ────────────────────────────────────────────────────

    def read_file(self, entry, path: str) -> bytes:
        status, resp = self._proxy(entry, "GET", f"/files?path={_q(path)}")
        if status != 200:
            raise _agent_error("read_file", status, resp)
        return resp

    def write_file(self, entry, path: str, data: bytes, mode: str = "0644") -> None:
        status, resp = self._proxy(entry, "POST", f"/files?path={_q(path)}&mode={mode}", data)
        if status not in (200, 201):
            raise _agent_error("write_file", status, resp)

    def stat_file(self, entry, path: str) -> FileInfo:
        status, resp = self._proxy(entry, "GET", f"/files/stat?path={_q(path)}")
        if status != 200:
            raise _agent_error("stat_file", status, resp)
        return _decode_file_info(json.loads(resp))

    def list_files(self, entry, path: str) -> list[FileInfo]:
        status, resp = self._proxy(entry, "GET", f"/files/list?path={_q(path)}")
        if status != 200:
            raise _agent_error("list_files", status, resp)
        payload = json.loads(resp)
        items = payload.get("entries", []) if isinstance(payload, dict) else payload
        return [_decode_file_info(e) for e in items]

    def mkdir(self, entry, path: str, parents: bool = True) -> None:
        status, resp = self._proxy(
            entry, "POST",
            f"/files/mkdir?path={_q(path)}&parents={'true' if parents else 'false'}",
        )
        if status not in (200, 201):
            raise _agent_error("mkdir", status, resp)

    def delete_file(self, entry, path: str, recursive: bool = False) -> None:
        status, resp = self._proxy(
            entry, "DELETE",
            f"/files?path={_q(path)}&recursive={'true' if recursive else 'false'}",
        )
        if status not in (200, 204):
            raise _agent_error("delete_file", status, resp)

    # ── SupportsPty ──────────────────────────────────────────────────────

    def create_process(self, entry, spec: ProcessSpec) -> ProcessHandle:
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

    def list_processes(self, entry) -> list[dict]:
        status, resp = self._proxy(entry, "GET", "/processes")
        if status != 200:
            raise _agent_error("list_processes", status, resp)
        payload = json.loads(resp)
        return payload.get("processes", payload) if isinstance(payload, dict) else payload

    def send_process_input(self, entry, pid: int, data: bytes) -> None:
        status, resp = self._proxy(entry, "POST", f"/processes/{pid}/input", data, timeout=5)
        if status not in (200, 204):
            raise _agent_error("send_process_input", status, resp)

    def signal_process(self, entry, pid: int, signal: int) -> None:
        body = json.dumps({"signal": signal}).encode()
        status, resp = self._proxy(entry, "POST", f"/processes/{pid}/signal", body, timeout=5)
        if status not in (200, 204):
            raise _agent_error("signal_process", status, resp)

    def resize_process(self, entry, pid: int, cols: int, rows: int) -> None:
        body = json.dumps({"cols": cols, "rows": rows}).encode()
        status, resp = self._proxy(entry, "POST", f"/processes/{pid}/resize", body, timeout=5)
        if status not in (200, 204):
            raise _agent_error("resize_process", status, resp)

    async def attach_terminal(self, entry, websocket) -> None:
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
                guest_ws_url = f"ws://{entry.guest_ip}:{AGENT_PORT}/processes/{guest_pid}/output"
                guest_ws = ws_sync.connect(guest_ws_url)
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

    # ── Internal: HTTP transport ────────────────────────────────────────

    def _proxy(self, entry, method: str, path: str, body: bytes | None = None, timeout: float = 65) -> tuple[int, bytes]:
        url = f"{entry.agent_url}{path}"
        req = urllib.request.Request(url, data=body, method=method)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def health(self, entry) -> tuple[bool, str | None]:
        handle = self._coerce_handle(entry)
        if handle is None:
            return False, "VM handle not found"
        # Check the VZ VM state directly — HTTP health may be unreachable over NAT.
        try:
            state = handle.vm.state()
            if state == 1:  # VZVirtualMachineStateRunning
                return True, None
            return False, f"VM state is {state}"
        except Exception as exc:
            return False, str(exc)

    def recover(self, row: dict) -> tuple[str, None]:
        pause_state_ref = row.get("pause_state_ref")
        if pause_state_ref and os.path.exists(pause_state_ref):
            return PAUSED, None
        return DEAD, None

    def build_template(self, name: str, source, **kwargs) -> dict:
        rootfs_size_mb = int(kwargs.get("rootfs_size_mb", 500))
        template_id = name.lower().replace(" ", "-")
        template_dir = os.path.join(TEMPLATES_DIR, template_id, self.kind)
        os.makedirs(template_dir, exist_ok=True)
        register_template_artifact(
            template_id,
            name,
            self.kind,
            template_dir,
            status="failed",
            rootfs_size_mb=rootfs_size_mb,
        )
        update_template_artifact_status(template_id, self.kind, "failed")
        raise NotImplementedError(
            "Custom macOS template building is not implemented yet. "
            "Use the default template with FLINT_VZ_KERNEL_PATH and FLINT_VZ_ROOTFS_PATH."
        )

    def delete_template_artifact(self, template_id: str, template: dict | None = None) -> None:
        if not template:
            return
        artifact = (template.get("artifacts") or {}).get(self.kind) or {}
        template_dir = artifact.get("template_dir")
        if template_dir and os.path.isdir(template_dir):
            shutil.rmtree(template_dir, ignore_errors=True)

    def _start_vm(
        self,
        *,
        vm_id: str,
        runtime_dir: str,
        disk_path: str,
        machine_id_path: str,
        boot_config: dict,
        console_log_path: str,
        state_path: str,
        resume_state_path: str | None,
    ) -> _MacVMHandle:
        import Virtualization as VZ
        from Foundation import NSDate, NSFileHandle, NSRunLoop, NSURL

        config = VZ.VZVirtualMachineConfiguration.alloc().init()

        config.setCPUCount_(int(boot_config.get("cpu_count", VZ_CPU_COUNT)))
        config.setMemorySize_(int(boot_config.get("memory_bytes", VZ_MEMORY_BYTES)))

        platform = VZ.VZGenericPlatformConfiguration.new()
        if os.path.exists(machine_id_path):
            with open(machine_id_path, "rb") as f:
                machine_id = VZ.VZGenericMachineIdentifier.alloc().initWithDataRepresentation_(f.read())
        else:
            machine_id = VZ.VZGenericMachineIdentifier.alloc().init()
            with open(machine_id_path, "wb") as f:
                f.write(machine_id.dataRepresentation())
        platform.setMachineIdentifier_(machine_id)
        config.setPlatform_(platform)

        bootloader = VZ.VZLinuxBootLoader.alloc().initWithKernelURL_(NSURL.fileURLWithPath_(boot_config["kernel_path"]))
        bootloader.setCommandLine_(boot_config.get("boot_args", VZ_BOOT_ARGS))
        initrd_path = boot_config.get("initrd_path")
        if initrd_path:
            bootloader.setInitialRamdiskURL_(NSURL.fileURLWithPath_(initrd_path))
        config.setBootLoader_(bootloader)

        attachment, error = VZ.VZDiskImageStorageDeviceAttachment.alloc().initWithURL_readOnly_error_(
            NSURL.fileURLWithPath_(disk_path),
            False,
            None,
        )
        if attachment is None:
            raise RuntimeError(f"Failed to open macOS VM disk image: {error}")
        block = VZ.VZVirtioBlockDeviceConfiguration.alloc().initWithAttachment_(attachment)
        config.setStorageDevices_([block])

        net = VZ.VZVirtioNetworkDeviceConfiguration.new()
        net.setAttachment_(VZ.VZNATNetworkDeviceAttachment.new())
        config.setNetworkDevices_([net])

        serial_in_r, serial_in_w = os.pipe()
        serial_out_r, serial_out_w = os.pipe()
        in_handle = NSFileHandle.alloc().initWithFileDescriptor_closeOnDealloc_(serial_in_r, True)
        out_handle = NSFileHandle.alloc().initWithFileDescriptor_closeOnDealloc_(serial_out_w, True)
        serial_attachment = VZ.VZFileHandleSerialPortAttachment.alloc().initWithFileHandleForReading_fileHandleForWriting_(
            in_handle,
            out_handle,
        )
        serial = VZ.VZVirtioConsoleDeviceSerialPortConfiguration.new()
        serial.setAttachment_(serial_attachment)
        config.setSerialPorts_([serial])
        config.setEntropyDevices_([VZ.VZVirtioEntropyDeviceConfiguration.new()])

        valid, error = config.validateWithError_(None)
        if not valid:
            raise RuntimeError(f"Invalid macOS VM configuration: {error}")

        vm = VZ.VZVirtualMachine.alloc().initWithConfiguration_(config)
        handle = _MacVMHandle(
            vm=vm,
            runtime_dir=runtime_dir,
            disk_path=disk_path,
            guest_ip="",
            console_log_path=console_log_path,
            reader_fd=serial_out_r,
            stdin_write_fd=serial_in_w,
            machine_id_path=machine_id_path,
            boot_config_path=os.path.join(runtime_dir, "boot.json"),
            state_path=state_path,
            config=config,
            # Prevent PyObjC from GC'ing these (which closes the pipe fds).
            _objc_refs=[config, in_handle, out_handle, serial_attachment, attachment],
        )

        ready_event = threading.Event()
        start_event = threading.Event()
        errors: dict[str, Any] = {}
        ip_holder: dict[str, str] = {}

        def _console_reader() -> None:
            os.makedirs(os.path.dirname(console_log_path), exist_ok=True)
            buffer = b""
            with open(console_log_path, "ab") as log_f:
                while not handle.stop_reading.is_set():
                    try:
                        chunk = os.read(serial_out_r, 4096)
                    except OSError:
                        break
                    if not chunk:
                        time.sleep(0.05)
                        continue
                    log_f.write(chunk)
                    log_f.flush()
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        text = line.decode(errors="replace").strip()
                        match = re.search(r"(?:FLINT_IP|IP)=([0-9.]+)", text)
                        if match:
                            ip_holder["ip"] = match.group(1)
                        if "READY" in text:
                            ready_event.set()

        read_thread = threading.Thread(target=_console_reader, daemon=True, name=f"vz-console-{vm_id[:8]}")
        handle.read_thread = read_thread
        read_thread.start()

        def _start_completion(error_obj) -> None:
            if error_obj is not None:
                errors["start"] = error_obj
            start_event.set()

        if resume_state_path:
            # restore -> VM is left in paused state -> resume to actually run.
            restore_event = threading.Event()
            restore_errors: dict[str, Any] = {}

            def _restore_done(error_obj=None) -> None:
                if error_obj is not None:
                    restore_errors["restore"] = error_obj
                restore_event.set()

            vm.restoreMachineStateFromURL_completionHandler_(
                NSURL.fileURLWithPath_(resume_state_path), _restore_done
            )
            restore_deadline = time.monotonic() + VZ_READY_TIMEOUT
            while not restore_event.is_set() and time.monotonic() < restore_deadline:
                NSRunLoop.currentRunLoop().runUntilDate_(
                    NSDate.dateWithTimeIntervalSinceNow_(0.1)
                )
            if not restore_event.is_set():
                raise RuntimeError("Timed out restoring macOS VM state")
            if restore_errors.get("restore") is not None:
                raise RuntimeError(f"Failed to restore macOS VM state: {restore_errors['restore']}")
            vm.resumeWithCompletionHandler_(_start_completion)
        else:
            vm.startWithCompletionHandler_(_start_completion)

        # Pump the RunLoop while waiting — VZ delivers the completion handler
        # and drives VM execution through it.
        from Foundation import NSRunLoop, NSDate

        deadline = time.monotonic() + VZ_READY_TIMEOUT
        while not start_event.is_set() and time.monotonic() < deadline:
            NSRunLoop.currentRunLoop().runUntilDate_(
                NSDate.dateWithTimeIntervalSinceNow_(0.1)
            )
        if not start_event.is_set():
            raise RuntimeError("Timed out waiting for macOS VM start")
        if errors.get("start") is not None:
            raise RuntimeError(f"Failed to start macOS VM: {errors['start']}")

        while time.monotonic() < deadline:
            NSRunLoop.currentRunLoop().runUntilDate_(
                NSDate.dateWithTimeIntervalSinceNow_(0.1)
            )
            if ready_event.is_set() and ip_holder.get("ip"):
                guest_ip = ip_holder["ip"]
                handle.guest_ip = guest_ip
                # Try the HTTP agent, but don't block creation if NAT prevents
                # host-to-guest connectivity — the VM is running either way.
                if self._wait_for_http_agent(guest_ip, timeout=5):
                    return handle
                # Agent unreachable (common with VZ NAT). Still return — the
                # VM booted and printed READY.
                return handle

        raise RuntimeError(
            "macOS VM started but guest agent never became ready. "
            "The guest init script must print FLINT_IP=<ipv4> and READY on the serial console."
        )

    def _stop_vm(self, handle: _MacVMHandle) -> None:
        from Foundation import NSRunLoop, NSDate

        event = threading.Event()
        errors: dict[str, Any] = {}

        def _done(error_obj=None) -> None:
            if error_obj is not None:
                errors["stop"] = error_obj
            event.set()

        vm = handle.vm
        if hasattr(vm, "canStop") and vm.canStop():
            vm.stopWithCompletionHandler_(_done)
        elif hasattr(vm, "canRequestStop") and vm.canRequestStop():
            err = vm.requestStopWithError_(None)
            if isinstance(err, tuple):
                _, possible_error = err
                if possible_error is not None:
                    errors["stop"] = possible_error
            event.set()
        else:
            event.set()

        deadline = time.monotonic() + 10
        while not event.is_set() and time.monotonic() < deadline:
            NSRunLoop.currentRunLoop().runUntilDate_(
                NSDate.dateWithTimeIntervalSinceNow_(0.1)
            )
        if errors.get("stop") is not None:
            raise RuntimeError(f"Failed to stop macOS VM: {errors['stop']}")

    def _suspend_vm(self, handle: _MacVMHandle) -> None:
        from Foundation import NSRunLoop, NSDate

        event = threading.Event()
        errors: dict[str, Any] = {}

        def _done(error_obj=None) -> None:
            if error_obj is not None:
                errors["pause"] = error_obj
            event.set()

        vm = handle.vm
        if hasattr(vm, "canPause") and not vm.canPause():
            raise RuntimeError("macOS VM cannot be paused in its current state")
        vm.pauseWithCompletionHandler_(_done)
        deadline = time.monotonic() + 10
        while not event.is_set() and time.monotonic() < deadline:
            NSRunLoop.currentRunLoop().runUntilDate_(
                NSDate.dateWithTimeIntervalSinceNow_(0.1)
            )
        if not event.is_set():
            raise RuntimeError("Timed out pausing macOS VM")
        if errors.get("pause") is not None:
            raise RuntimeError(f"Failed to pause macOS VM: {errors['pause']}")

    def _save_vm_state(self, handle: _MacVMHandle) -> None:
        from Foundation import NSRunLoop, NSDate, NSURL

        event = threading.Event()
        errors: dict[str, Any] = {}

        def _done(error_obj=None) -> None:
            if error_obj is not None:
                errors["save"] = error_obj
            event.set()

        config = handle.config
        if config is None:
            raise RuntimeError("VM handle is missing its configuration; cannot validate save/restore support")
        valid, error = config.validateSaveRestoreSupportWithError_(None)
        if not valid:
            raise RuntimeError(f"macOS VM save/restore unsupported: {error}")

        handle.vm.saveMachineStateToURL_completionHandler_(NSURL.fileURLWithPath_(handle.state_path), _done)
        deadline = time.monotonic() + VZ_READY_TIMEOUT
        while not event.is_set() and time.monotonic() < deadline:
            NSRunLoop.currentRunLoop().runUntilDate_(
                NSDate.dateWithTimeIntervalSinceNow_(0.1)
            )
        if not event.is_set():
            raise RuntimeError("Timed out saving macOS VM state")
        if errors.get("save") is not None:
            raise RuntimeError(f"Failed to save macOS VM state: {errors['save']}")

    @staticmethod
    def _wait_for_http_agent(guest_ip: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"http://{guest_ip}:{AGENT_PORT}/health", timeout=1.0) as resp:
                    if resp.status == 200:
                        return True
            except Exception:
                time.sleep(0.1)
        return False

    @staticmethod
    def _coerce_handle(entry) -> _MacVMHandle | None:
        handle = entry.process
        if isinstance(handle, _MacVMHandle):
            return handle
        return None

    @staticmethod
    def _artifact_config_path(template_dir: str) -> str:
        return os.path.join(template_dir, "artifact.json")

    def _write_artifact_config(self, template_dir: str, payload: dict[str, Any]) -> None:
        with open(self._artifact_config_path(template_dir), "w") as f:
            json.dump(payload, f)

    def _load_artifact_config(self, template_dir: str) -> dict[str, Any] | None:
        return self._read_json(self._artifact_config_path(template_dir))

    @staticmethod
    def _read_json(path: str) -> dict[str, Any] | None:
        if not os.path.exists(path):
            return None
        with open(path, "r") as f:
            return json.load(f)

    @staticmethod
    def _default_assets_ready() -> bool:
        return os.path.exists(VZ_KERNEL_PATH) and os.path.exists(VZ_ROOTFS_PATH)


# ── Module-private decoders ────────────────────────────────────────────────


def _q(s: str) -> str:
    import urllib.parse as _u
    return _u.quote(s, safe="/")


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

"""Bounded build admission and a host-supervised, disposable worker process."""
from __future__ import annotations

import contextvars
import ctypes
import json
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import sys
import threading
import uuid

from . import config

_LIMIT = int(os.environ.get("FLINT_BUILD_CONCURRENCY", "1"))
_TIMEOUT = int(os.environ.get("FLINT_BUILD_TIMEOUT_SECONDS", "900"))
if not 1 <= _LIMIT <= 4 or _TIMEOUT <= 0:
    raise ValueError("Build concurrency must be 1..4 and timeout must be positive")
_slots = threading.BoundedSemaphore(_LIMIT)
_admitted = contextvars.ContextVar("build_admitted", default=False)


class BuildBusy(RuntimeError):
    pass


class Admission:
    def __init__(self):
        for manifest in _root().glob("*/job.json"):
            try:
                pending = json.loads(manifest.read_text()).get("cleanup_pending")
            except FileNotFoundError:
                continue
            if pending:
                raise BuildBusy("A previous build still needs cleanup; restart the daemon to retry it")
        if not _slots.acquire(blocking=False):
            raise BuildBusy("Build capacity is full; retry later")
        self.released = False

    def __enter__(self):
        self.token = _admitted.set(True)
        return self

    def __exit__(self, *args):
        _admitted.reset(self.token)
        self.release()

    def release(self):
        if not self.released:
            self.released = True
            _slots.release()


def _root() -> Path:
    root = Path(config.STATE_DIR) / "build-jobs"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


def _atomic(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as stream:
        os.chmod(tmp, 0o600)
        json.dump(data, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def register_vm(vm_id: str, ns: str, kind: str) -> None:
    """Journal ownership before creating any guest resource."""
    job = os.environ.get("FLINT_BUILD_JOB")
    if job:
        directory = _root() / job / "resources"
        directory.mkdir(mode=0o700, exist_ok=True)
        _atomic(directory / (vm_id + ".json"), {"vm_id": vm_id, "ns": ns, "kind": kind})


def output_dir(template_id: str) -> str:
    job = os.environ.get("FLINT_BUILD_JOB")
    return str(Path(config.TEMPLATES_DIR) / (".build-" + job if job else template_id))


def _cleanup(job: Path) -> None:
    from ._limits import stop_processes, vm_cgroup
    from .net.netns import _delete_netns
    from .vmm.firecracker.jail import FirecrackerJail
    from .vmm.cloud_hypervisor.jail import CloudHypervisorJail
    # Stop the worker and all its host subprocesses before touching resources.
    group = f"/sys/fs/cgroup/flint-builds/{job.name}"
    stop_processes(group)
    for marker in sorted((job / "resources").glob("*.json")):
        data = json.loads(marker.read_text())
        vm_id, ns, kind = data["vm_id"], data["ns"], data["kind"]
        if not re.fullmatch(r"(?:build-|golden-)[a-f0-9-]+", vm_id) or not re.fullmatch(r"[a-z0-9-]+", ns):
            raise RuntimeError("Invalid build recovery record")
        stop_processes(vm_cgroup(vm_id, kind))
        _delete_netns(ns)
        if kind == "linux-firecracker":
            FirecrackerJail(vm_id).cleanup()
        elif kind == "linux-cloud-hypervisor":
            CloudHypervisorJail(vm_id).cleanup()
        else:
            raise RuntimeError("Invalid builder backend")
        (Path(config.STATE_DIR) / "builders" / (vm_id + ".json")).unlink(missing_ok=True)
        from ._wipe import take
        take(vm_id)
        marker.unlink()
    if Path(group).exists():
        os.rmdir(group)
    if shutil.which("docker"):
        subprocess.run(["docker", "rm", "-f", "flint-appliance-" + job.name], capture_output=True, timeout=30)
    from ._wipe import wipe_tree
    for directory in (Path(config.DATA_DIR) / ".builder").glob("prepare-" + job.name + "-*"):
        wipe_tree(str(directory))


def _finish(job: Path) -> None:
    # Keep the durable recovery record intact until every private child is
    # wiped. A crash during log wiping must not corrupt that record.
    from ._wipe import wipe_tree
    for child in job.iterdir():
        if child.name != "job.json":
            wipe_tree(str(child))
    (job / "job.json").unlink()
    os.rmdir(job)


def recover_builds() -> None:
    from ._wipe import wipe_tree
    from ._template_registry import register_template_artifact, update_template_artifact_status
    for job in _root().iterdir():
        if not re.fullmatch(r"[a-f0-9]{32}", job.name):
            raise RuntimeError("Invalid build operation directory")
        if not (job / "job.json").exists():
            wipe_tree(str(job))  # no worker can start before this journal exists
            continue
        data = json.loads((job / "job.json").read_text())
        _cleanup(job)
        stage = Path(config.TEMPLATES_DIR) / (".build-" + job.name)
        retired = Path(config.TEMPLATES_DIR) / (".retired-" + job.name)
        destination = Path(config.TEMPLATES_DIR) / data["template_id"]
        if data.get("publishing") and not data.get("published"):
            if stage.exists():
                # Interrupted before the new template moved in: restore the old one.
                if retired.exists() and not destination.exists():
                    os.replace(retired, destination)
            elif destination.exists():
                # The new template is in place: finish publishing it.
                register_template_artifact(data["template_id"], data.get("name", data["template_id"]), data["kind"],
                                           str(destination), status="ready", rootfs_size_mb=data.get("rootfs_size_mb"))
                data["published"] = True
        wipe_tree(str(stage))
        wipe_tree(str(retired))
        if not data.get("published"):
            update_template_artifact_status(data["template_id"], data["kind"], "failed")
        _finish(job)


def _supervise(command: list[str], body: bytes, log, env: dict, timeout: float) -> int:
    """A wall-clock deadline, independent of what the worker sends or receives."""
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT, env=env)
    try:
        process.communicate(body, timeout=timeout)
        return process.returncode
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"Template build exceeded host deadline of {timeout}s") from exc
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


def run_build(kind: str, name: str, dockerfile: str, size_mb: int) -> dict:
    if not _admitted.get():
        with Admission():
            return run_build(kind, name, dockerfile, size_mb)
    from ._template_build import _slugify
    from ._template_registry import register_template_artifact, update_template_artifact_status
    from ._wipe import wipe_tree
    from ._limits import cgroup_limits
    from ._artifacts import fsync_path
    from .net.netns import _allocate_veth_ip, _release_veth_ip
    template_id = _slugify(name)
    job = _root() / uuid.uuid4().hex
    job.mkdir(mode=0o700)
    journal = {"kind": kind, "template_id": template_id, "name": name, "rootfs_size_mb": size_mb}
    _atomic(job / "job.json", journal)
    group = f"/sys/fs/cgroup/flint-builds/{job.name}"
    stage = Path(config.TEMPLATES_DIR) / (".build-" + job.name)
    retired = stage.with_name(".retired-" + job.name)
    # Only this process's allocator knows the bridge IPs of live sandboxes.
    reservation = "build-" + job.name
    veth_ip = _allocate_veth_ip(reservation)
    try:
        cgroup_limits(group, config.STATE_DIR, vcpus=2, memory_mib=2048, vmm_disk_limit=True)
        env = {**os.environ, "FLINT_BUILD_JOB": job.name, "FLINT_BUILD_PARENT": str(os.getpid()),
               "FLINT_BUILD_VETH_IP": veth_ip}
        with (job / "worker.log").open("wb") as log:
            result = _supervise([sys.executable, "-m", __name__],
                                json.dumps({"kind": kind, "name": name, "dockerfile": dockerfile,
                                            "rootfs_size_mb": size_mb}).encode(), log, env, _TIMEOUT)
        if result:
            with (job / "worker.log").open("rb") as log:
                log.seek(0, 2)
                log.seek(max(0, log.tell() - 4096))
                raise RuntimeError(log.read().decode(errors="replace"))
        _cleanup(job)
        _release_veth_ip(reservation)
        # Recovery rolls an interrupted swap back (old template restored) or
        # forward (new template registered); it never discards both.
        _atomic(job / "job.json", {**journal, "publishing": True})
        destination = Path(config.TEMPLATES_DIR) / template_id
        if destination.exists():
            os.replace(destination, retired)
        os.replace(stage, destination)
        fsync_path(config.TEMPLATES_DIR)
        register_template_artifact(template_id, name, kind, str(destination), status="ready", rootfs_size_mb=size_mb)
        _atomic(job / "job.json", {**journal, "published": True})
        wipe_tree(str(retired))
        _finish(job)
        return {"template_id": template_id, "status": "ready"}
    except Exception as exc:
        # Retain the operation journal on cleanup failure, for startup recovery.
        update_template_artifact_status(template_id, kind, "failed", error=str(exc))
        try:
            _cleanup(job)
            destination = Path(config.TEMPLATES_DIR) / template_id
            if retired.exists() and not destination.exists():
                os.replace(retired, destination)  # failed mid-swap: keep the old template
            wipe_tree(str(stage))
            wipe_tree(str(retired))
        except Exception:
            # The builder namespace may still hold the IP: keep it reserved.
            _atomic(job / "job.json", {**journal, "cleanup_pending": True})
            raise
        _release_veth_ip(reservation)
        _finish(job)
        raise


def _worker() -> None:
    # If the daemon dies, the worker cannot publish a late success. Its cgroup
    # and pre-created journal let the next daemon stop any remaining children.
    ctypes.CDLL(None).prctl(1, signal.SIGKILL, 0, 0, 0)
    if os.getppid() != int(os.environ["FLINT_BUILD_PARENT"]):
        raise RuntimeError("Build supervisor exited")
    from ._limits import attach_process
    attach_process(f"/sys/fs/cgroup/flint-builds/{os.environ['FLINT_BUILD_JOB']}", os.getpid())
    request = json.load(sys.stdin)
    from .backends.registry import get_backend
    backend = get_backend(request.pop("kind"))
    backend.build_template(**request)
    from ._artifacts import fsync_path
    from ._template_build import _slugify
    for path in Path(output_dir(_slugify(request["name"]))).rglob("*"):
        if path.is_file():
            fsync_path(str(path))


if __name__ == "__main__":
    _worker()

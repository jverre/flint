"""Untrusted Dockerfiles execute in disposable, resource-limited microVMs.

The host Docker daemon only prepares the fixed, trusted builder appliance.
Neither submitted Dockerfiles nor their exported files execute on the host.
Each build boots a private disk with an empty Docker cache. The artifact is
created inside the builder and copied as a bounded byte stream, never mounted
or extracted on the host.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import re
import urllib.request
import uuid

from . import config
from ._artifacts import sparse_copy, shared_readonly
from ._firecracker import _fc_put, _fc_status_ok, _wait_for_api_socket, _wait_for_agent
from ._jailer import JailSpec, build_jailer_command, cleanup_jailer, make_private
from ._netns import (_setup_netns_pyroute2, _apply_netns_firewall, _strip_ns_prefix,
                     _enter_netns, _restore_netns, _delete_netns, open_tap_fd)

_bootstrap_lock = threading.Lock()
_concurrency = int(os.environ.get("FLINT_BUILD_CONCURRENCY", "1"))
if not 1 <= _concurrency <= 4:
    raise ValueError("FLINT_BUILD_CONCURRENCY must be between 1 and 4")
_build_slots = threading.BoundedSemaphore(_concurrency)
_MEMORY_MIB = 2048
_CPUS = 2
_TIMEOUT = 900
_APPLIANCE_DOCKERFILE = """FROM alpine:3.21
RUN apk add --no-cache docker docker-cli-buildx iproute2 e2fsprogs util-linux coreutils tar
COPY flintd /usr/local/bin/flintd
COPY init-net.sh /etc/init-net.sh
RUN chmod +x /etc/init-net.sh /usr/local/bin/flintd
"""


def _marker(vm_id: str) -> Path:
    directory = Path(config.STATE_DIR) / "builders"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory / (vm_id + ".json")


def cleanup_abandoned_builders() -> None:
    """Called once before accepting builds after a daemon restart."""
    from ._ch_jail import ChJail
    for marker in _marker("unused").parent.glob("build-*.json"):
        vm_id = marker.stem
        data = json.loads(marker.read_text())
        if not re.fullmatch(r"build-[0-9a-f]{16}", vm_id) or not re.fullmatch(r"fb-[0-9a-f]{8}", data["ns"]):
            raise RuntimeError(f"Invalid builder recovery record: {marker}")
        spec = JailSpec(vm_id, data["ns"]) if data["kind"] == "linux-firecracker" else None
        jail = None if spec else ChJail(vm_id)
        group = Path(f"/sys/fs/cgroup/{config.JAILER_PARENT_CGROUP}/{vm_id}" if spec else jail.cgroup)
        if group.exists():
            # The cgroup is unique to this appliance; never kill a reused PID.
            (group / "cgroup.kill").write_text("1")
            deadline = time.monotonic() + 10
            while (group / "cgroup.procs").read_text().strip():
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"Builder {vm_id} has not stopped; retaining its disk")
                time.sleep(.02)
        _delete_netns(data["ns"])
        cleanup_jailer(spec.chroot_base, vm_id) if spec else jail.cleanup()
        marker.unlink()


def _appliance() -> str:
    # The content key avoids reusing an appliance with an old guest agent.
    from ._template_build import _build_flintd, _find_init_net_sh, _extract_rootfs
    root = Path(config.DATA_DIR) / ".builder"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with _bootstrap_lock, tempfile.TemporaryDirectory(prefix="bootstrap-", dir=root) as context:
        _build_flintd(os.path.join(context, "flintd"))
        shutil.copy2(_find_init_net_sh(), os.path.join(context, "init-net.sh"))
        key = hashlib.sha256(_APPLIANCE_DOCKERFILE.encode() + Path(context, "flintd").read_bytes()
                             + Path(context, "init-net.sh").read_bytes()).hexdigest()[:16]
        path = root / f"{key}.ext4"
        if path.exists():
            return str(path)
        Path(context, "Dockerfile").write_text(_APPLIANCE_DOCKERFILE)
        tag = f"flint-builder:{key}"
        try:
            subprocess.run(["docker", "build", "-t", tag, context], check=True, capture_output=True, timeout=300)
            _extract_rootfs(tag, str(path) + ".tmp", 4096)
            os.chmod(str(path) + ".tmp", 0o600)
            os.replace(str(path) + ".tmp", path)
        finally:
            subprocess.run(["docker", "image", "rm", tag], capture_output=True, timeout=30)
        return str(path)


@contextmanager
def _vm(kind: str, disk_mb: int):
    source = _appliance()
    vm_id = "build-" + uuid.uuid4().hex[:16]
    ns = "fb-" + uuid.uuid4().hex[:8]
    tap = "tap-build"
    process = None
    jail = None
    spec = None
    marker = _marker(vm_id)
    marker.write_text(json.dumps({"ns": ns, "kind": kind}))
    os.chmod(marker, 0o600)
    try:
        if kind == "linux-firecracker":
            spec = JailSpec(vm_id, ns)
            os.makedirs(spec.chroot_root)
            os.chown(spec.chroot_root, config.JAILER_UID, config.JAILER_GID)
            rootfs = f"{spec.chroot_root}/rootfs.ext4"
        else:
            from ._ch_jail import ChJail
            jail = ChJail(vm_id)
            jail.prepare()
            rootfs = jail.host_path("/rootfs.ext4")
        sparse_copy(source, rootfs)
        make_private(rootfs) if spec else jail.give("/rootfs.ext4")
        # Allocate enough sparse space for Docker's vfs copies, export and output.
        with open(rootfs, "r+b") as f:
            f.truncate(max(12288, disk_mb * 4 + 4096) * (1 << 20))
        subprocess.run(["resize2fs", rootfs], check=True, capture_output=True, timeout=60)
        _setup_netns_pyroute2(ns, tap)
        _apply_netns_firewall(ns, "vn-" + _strip_ns_prefix(ns)[:8], allow_storage=False)
        if spec:
            os.link(shared_readonly(config.KERNEL_PATH, config.JAILER_SHARED_DIR), f"{spec.chroot_root}/vmlinux")
            with open(f"{spec.chroot_base}/builder.log", "w") as log:
                process = subprocess.Popen(build_jailer_command(spec, vcpus=_CPUS, memory_mib=_MEMORY_MIB),
                                           stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
            _wait_for_api_socket(spec.socket_path_on_host)
            requests = [("/machine-config", {"vcpu_count": _CPUS, "mem_size_mib": _MEMORY_MIB}),
                        ("/boot-source", {"kernel_image_path": "vmlinux", "boot_args": config.BOOT_ARGS}),
                        ("/drives/rootfs", {"drive_id": "rootfs", "path_on_host": "rootfs.ext4", "is_root_device": True, "is_read_only": False}),
                        ("/network-interfaces/eth0", {"iface_id": "eth0", "guest_mac": config.GUEST_MAC, "host_dev_name": tap}),
                        ("/actions", {"action_type": "InstanceStart"})]
            for route, body in requests:
                if not _fc_status_ok(_fc_put(spec.socket_path_on_host, route, body)):
                    raise RuntimeError(f"Builder VM failed at {route}")
        else:
            from . import _cloud_hypervisor as ch
            from ._ch_boot import CH_BOOT_ARGS
            process = jail.spawn(ns, vcpus=_CPUS, memory_mib=_MEMORY_MIB)
            ch.vm_create(jail.socket_path, {
                "cpus": {"boot_vcpus": _CPUS, "max_vcpus": _CPUS},
                "memory": {"size": _MEMORY_MIB << 20},
                "payload": {"kernel": "/vmlinux", "cmdline": CH_BOOT_ARGS},
                "disks": [{"path": "/rootfs.ext4"}], "serial": {"mode": "Tty"}, "console": {"mode": "Off"}})
            fd = open_tap_fd(ns, tap)
            try:
                ch.vm_add_net(jail.socket_path, {"id": "eth0", "mac": config.GUEST_MAC}, fd)
            finally:
                os.close(fd)
            ch.vm_boot(jail.socket_path)
        _wait_for_agent(ns, retries=3000)
        yield ns
    finally:
        if process:
            process.kill()
            process.wait(timeout=10)
        _delete_netns(ns)
        if spec:
            cleanup_jailer(spec.chroot_base, vm_id)
        elif jail:
            jail.cleanup()
        marker.unlink(missing_ok=True)


def _request(ns: str, path: str, data: bytes | None = None, timeout: int = 60):
    old = _enter_netns(ns)
    try:
        req = urllib.request.Request(f"http://{config.GUEST_IP}:{config.AGENT_PORT}{path}", data=data)
        return urllib.request.urlopen(req, timeout=timeout)
    finally:
        _restore_netns(old)


def build_rootfs(kind: str, dockerfile: str, context: str, output: str, size_mb: int) -> None:
    """Build an artifact inside a fresh VM, and wipe the builder on every path."""
    if kind not in ("linux-firecracker", "linux-cloud-hypervisor"):
        raise ValueError("Isolated template builds require a Linux hypervisor")
    with _build_slots, _vm(kind, size_mb) as ns:
        def run(cmd: str, timeout: int = 60):
            body = json.dumps({"cmd": ["/bin/sh", "-ec", cmd], "timeout": timeout}).encode()
            with _request(ns, "/exec", body, timeout + 10) as response:
                raw = response.read((1 << 20) + 1)
                if len(raw) > 1 << 20:
                    raise RuntimeError("Builder command output exceeded 1 MiB")
                result = json.loads(raw)
            if result["exit_code"] != 0:
                raise RuntimeError(f"Template build failed in builder VM: {result.get('stderr', '')[-2000:]}")

        run("mkdir -p /build /export /run /sys/fs/cgroup; "
            "mount -t cgroup2 none /sys/fs/cgroup 2>/dev/null || true; "
            "dockerd --storage-driver=vfs --iptables=false --bridge=none --ip-forward=false --ip-masq=false "
            ">/var/log/docker.log 2>&1 &")
        run("i=0; until docker info >/dev/null 2>&1; do i=$((i+1)); "
            "if [ $i -ge 60 ]; then cat /var/log/docker.log >&2; exit 1; fi; sleep .5; done")
        for name in ("flintd", "init-net.sh"):
            with _request(ns, f"/files?path=/build/{name}", Path(context, name).read_bytes()) as response:
                response.read()
        with _request(ns, "/files?path=/build/Dockerfile", dockerfile.encode()) as response:
            response.read()
        # --network=host is confined to this disposable VM, never the host.
        run("DOCKER_BUILDKIT=1 docker build --network=host --no-cache -t artifact /build && "
            "docker create --name artifact artifact >/dev/null && "
            "docker export artifact > /export.tar && tar -xf /export.tar -C /export && "
            f"truncate -s {size_mb}M /artifact.ext4 && mkfs.ext4 -F -m 0 -d /export /artifact.ext4", _TIMEOUT)
        expected = size_mb << 20
        tmp = output + ".tmp"
        try:
            with _request(ns, "/files?path=/artifact.ext4", timeout=300) as response, open(tmp, "xb") as dest:
                left = expected
                while left:
                    chunk = response.read(min(left, 1 << 20))
                    if not chunk:
                        raise RuntimeError("Builder returned a truncated rootfs")
                    dest.write(chunk)
                    left -= len(chunk)
                if response.read(1):
                    raise RuntimeError("Builder returned an oversized rootfs")
                dest.flush()
                os.fsync(dest.fileno())
            os.replace(tmp, output)
        finally:
            if os.path.exists(tmp):
                from ._wipe import wipe_tree
                wipe_tree(tmp)

"""Template images are pulled and converted in disposable, resource-limited microVMs.

Neither image layers nor their files are parsed, extracted or executed on the
host. Each import boots a private copy of the builder appliance (the default
rootfs plus e2fsprogs, GNU tar and a static busybox), pulls and flattens the
image there, adds Flint's guest files, and builds the ext4 root disk. The disk
is copied out as a bounded byte stream, never mounted or extracted on the host.
"""
from __future__ import annotations

from contextlib import contextmanager
import gzip
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import re
import urllib.request
import uuid

from . import config
from ._limits import ch_disk_limiter
from ._artifacts import sparse_copy, shared_readonly
from ._firecracker import _fc_put, _fc_status_ok, _wait_for_api_socket, _wait_for_agent
from ._jailer import JailSpec, build_jailer_command, cleanup_jailer, make_private
from ._netns import (_setup_netns_pyroute2, _apply_netns_firewall, _strip_ns_prefix,
                     _enter_netns, _restore_netns, _delete_netns, open_tap_fd)

_MEMORY_MIB = 2048
_CPUS = 2
_TIMEOUT = 900
# Added to the default rootfs to make the builder appliance (Alpine's signed packages).
_APPLIANCE_PACKAGES = "e2fsprogs tar busybox-static"


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
        from ._wipe import take
        take(vm_id)
        marker.unlink()


def _appliance(kind: str) -> str:
    """The builder disk: the trusted default rootfs plus Alpine packages, added in a VM.

    Keyed by the default rootfs and the package list, so it is prepared once per
    rootfs version and never with host Docker.
    """
    st = os.stat(config.SOURCE_ROOTFS)
    key = hashlib.sha256(f"{config.SOURCE_ROOTFS}:{st.st_size}:{st.st_mtime_ns}:{_APPLIANCE_PACKAGES}".encode()).hexdigest()[:16]
    root = Path(config.DATA_DIR) / ".builder"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = root / f"{key}.ext4"
    with (root / ".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists():
            return str(path)
        started = time.monotonic()
        config.log.info("Builder appliance %s: preparing from %s", key, config.SOURCE_ROOTFS)
        tmp = root / f"prepare-{os.environ['FLINT_BUILD_JOB']}-{key}.ext4"
        try:
            with _vm(kind, 1024, source=config.SOURCE_ROOTFS, keep=str(tmp)) as ns:
                _run(ns, f"apk add --no-cache {_APPLIANCE_PACKAGES} && sync", 300, stage="add builder packages")
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                from ._wipe import wipe_tree
                wipe_tree(str(tmp))
        config.log.info("Builder appliance %s: ready in %.1fs", key, time.monotonic() - started)
        return str(path)


@contextmanager
def _vm(kind: str, disk_mb: int, *, source: str | None = None, keep: str | None = None):
    """Boot a builder VM; ``keep`` copies its disk out after it stops."""
    source = source or _appliance(kind)
    vm_id = "build-" + uuid.uuid4().hex[:16]
    ns = "fb-" + uuid.uuid4().hex[:8]
    tap = "tap-build"
    process = None
    jail = None
    spec = None
    marker = _marker(vm_id)
    from ._build_job import register_vm, _atomic
    register_vm(vm_id, ns, kind)
    _atomic(marker, {"ns": ns, "kind": kind})
    os.chmod(marker, 0o600)
    started = time.monotonic()
    config.log.info("Builder %s: starting %s VM", vm_id, kind)
    try:
        if kind == "linux-firecracker":
            spec = JailSpec(vm_id, ns)
            os.makedirs(spec.chroot_root)
            os.chown(spec.chroot_root, *spec.identity)
            rootfs = f"{spec.chroot_root}/rootfs.ext4"
        else:
            from ._ch_jail import ChJail
            jail = ChJail(vm_id)
            jail.prepare()
            rootfs = jail.host_path("/rootfs.ext4")
        sparse_copy(source, rootfs)
        make_private(rootfs, vm_id) if spec else jail.give("/rootfs.ext4")
        # Bound space for the flattened image and the output filesystem.
        with open(rootfs, "r+b") as f:
            f.truncate(max(12288, disk_mb * 4 + 4096) * (1 << 20))
        subprocess.run(["resize2fs", rootfs], check=True, capture_output=True, timeout=60)
        # The supervising daemon reserved this bridge IP against its live sandboxes.
        _setup_netns_pyroute2(ns, tap, veth_ip=os.environ["FLINT_BUILD_VETH_IP"])
        _apply_netns_firewall(ns, "vn-" + _strip_ns_prefix(ns)[:8], allow_storage=False)
        if spec:
            os.link(shared_readonly(config.KERNEL_PATH, config.JAILER_SHARED_DIR), f"{spec.chroot_root}/vmlinux")
            with open(f"{spec.chroot_base}/builder.log", "w") as log:
                process = subprocess.Popen(build_jailer_command(spec, vcpus=_CPUS, memory_mib=_MEMORY_MIB),
                                           stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
            _wait_for_api_socket(spec.socket_path_on_host, process=process, log_path=f"{spec.chroot_base}/builder.log", vm_id=spec.vm_id)
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
                "disks": [{"path": "/rootfs.ext4", "rate_limiter_config": ch_disk_limiter()}], "serial": {"mode": "Tty"}, "console": {"mode": "Off"}})
            fd = open_tap_fd(ns, tap)
            try:
                ch.vm_add_net(jail.socket_path, {"id": "eth0", "mac": config.GUEST_MAC}, fd)
            finally:
                os.close(fd)
            ch.vm_boot(jail.socket_path)
        _wait_for_agent(ns, retries=3000)
        config.log.info("Builder %s: guest ready in %.1fs", vm_id, time.monotonic() - started)
        yield ns
        if keep:
            process.kill()
            process.wait(timeout=10)
            process = None
            sparse_copy(rootfs, keep)
    finally:
        if process:
            process.kill()
            process.wait(timeout=10)
        _delete_netns(ns)
        if spec:
            cleanup_jailer(spec.chroot_base, vm_id)
        elif jail:
            jail.cleanup()
        from ._wipe import take
        take(vm_id)
        marker.unlink(missing_ok=True)


def _request(ns: str, path: str, data: bytes | None = None, timeout: int = 60):
    old = _enter_netns(ns)
    try:
        req = urllib.request.Request(f"http://{config.GUEST_IP}:{config.AGENT_PORT}{path}", data=data)
        return urllib.request.urlopen(req, timeout=timeout)
    finally:
        _restore_netns(old)


class _BoundedReader:
    def __init__(self, response, maximum: int):
        self.response, self.remaining = response, maximum

    def read(self, size=-1):
        data = self.response.read(min(size if size >= 0 else self.remaining + 1, self.remaining + 1))
        self.remaining -= len(data)
        if self.remaining < 0:
            raise RuntimeError("Builder compressed artifact exceeded its size bound")
        return data


def _run(ns: str, cmd: str, timeout: int = 60, *, stage: str) -> None:
    started = time.monotonic()
    config.log.info("Builder %s: %s", ns, stage)
    body = json.dumps({"cmd": ["/bin/sh", "-ec", cmd], "timeout": timeout}).encode()
    with _request(ns, "/exec", body, timeout + 10) as response:
        raw = response.read((1 << 20) + 1)
        if len(raw) > 1 << 20:
            raise RuntimeError("Builder command output exceeded 1 MiB")
        result = json.loads(raw)
    if result["exit_code"] != 0:
        detail = (result.get("stdout", "") + result.get("stderr", "")).strip()[-4096:]
        raise RuntimeError(f"Template import failed to {stage}: {detail}")
    config.log.info("Builder %s: %s completed in %.1fs", ns, stage, time.monotonic() - started)


def _upload(ns: str, path: str, data: bytes) -> None:
    with _request(ns, f"/files?path={path}", data) as response:
        response.read()


def _download_small(ns: str, path: str, limit: int = 1 << 16) -> bytes:
    with _request(ns, f"/files?path={path}") as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise RuntimeError(f"Builder file {path} exceeded {limit} bytes")
    return data


def import_rootfs(kind: str, image: str, auth: dict | None, context: str, output: str, size_mb: int) -> dict:
    """Pull ``image`` and build its root disk inside a fresh VM; return its source.

    The builder is wiped on every path. ``auth`` reaches only the importer,
    which deletes it once read.
    """
    if kind not in ("linux-firecracker", "linux-cloud-hypervisor"):
        raise ValueError("Template imports require a Linux hypervisor")
    import shlex
    with _vm(kind, size_mb) as ns:
        for name in ("flint-import", "flintd", "init-net.sh", "template-inject.sh"):
            _upload(ns, f"/build/{name}", Path(context, name).read_bytes())
        auth_arg = ""
        if auth:
            _upload(ns, "/build/auth.json", json.dumps(auth).encode())
            auth_arg = "-auth /build/auth.json "
        # GNU tar refuses absolute and ../ members and never writes through a
        # symlink the archive itself created.
        # On failure, report the importer's own error rather than tar's
        # complaint about the stream it cut short.
        _run(ns, "chmod 755 /build/flint-import; mkdir -p /export; chmod 755 /export; set -o pipefail; "
                 f"/build/flint-import -image {shlex.quote(image)} {auth_arg}-max-bytes {size_mb << 20} "
                 "-source-out /build/source.json -env-out /build/image.env 2>/build/import.err "
                 "| tar -x --numeric-owner --xattrs --xattrs-include='*' -C /export 2>/build/tar.err "
                 "|| { if [ -s /build/import.err ]; then cat /build/import.err; else cat /build/tar.err; fi >&2; exit 1; }",
             _TIMEOUT, stage="pull the image")
        source = json.loads(_download_small(ns, "/build/source.json"))
        # A fresh directory the archive cannot have pre-planted, then a chroot
        # so the image's own symlinks resolve inside the image.
        _run(ns, "rm -rf /export/.flint-inject; mkdir -m 700 /export/.flint-inject; "
                 "cp /bin/busybox.static /export/.flint-inject/busybox; "
                 "cp /build/flintd /build/init-net.sh /build/image.env /build/template-inject.sh /export/.flint-inject/; "
                 "chroot /export /.flint-inject/busybox sh /.flint-inject/template-inject.sh; "
                 "rm -rf /export/.flint-inject", 120, stage="add Flint's guest files")
        _run(ns, f"used=$(du -sm /export | cut -f1); if [ $((used + used / 10 + 16)) -gt {size_mb} ]; then "
                 f"echo \"the image needs about $((used + used / 10 + 16)) MiB; raise rootfs_size_mb above {size_mb}\" >&2; exit 1; fi; "
                 f"truncate -s {size_mb}M /artifact.ext4 && mkfs.ext4 -q -F -m 0 -d /export /artifact.ext4 && "
                 "gzip -1 /artifact.ext4", _TIMEOUT, stage="build the root disk")
        expected = size_mb << 20
        tmp = output + ".tmp"
        started = time.monotonic()
        config.log.info("Builder %s: downloading rootfs (%d MiB uncompressed)", ns, size_mb)
        try:
            # Empty ext4 space compresses well: do not send the full logical
            # disk over the guest's rate-limited link. Decompression remains
            # bounded by the exact expected artifact size below.
            with _request(ns, "/files?path=/artifact.ext4.gz", timeout=300) as response, \
                    gzip.GzipFile(fileobj=_BoundedReader(response, expected + expected // 100 + 65536)) as artifact, open(tmp, "xb") as dest:
                left = expected
                while left:
                    chunk = artifact.read(min(left, 1 << 20))
                    if not chunk:
                        raise RuntimeError("Builder returned a truncated rootfs")
                    dest.write(chunk)
                    left -= len(chunk)
                if artifact.read(1):
                    raise RuntimeError("Builder returned an oversized rootfs")
                dest.flush()
                os.fsync(dest.fileno())
            os.replace(tmp, output)
            config.log.info("Builder %s: rootfs downloaded in %.1fs", ns, time.monotonic() - started)
        finally:
            if os.path.exists(tmp):
                from ._wipe import wipe_tree
                wipe_tree(tmp)
    if not isinstance(source, dict) or not isinstance(source.get("image"), str) or not isinstance(source.get("digest"), str) \
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", source["digest"]) or len(source["image"]) > 1024:
        raise RuntimeError("Builder returned an invalid image source")
    return {"image": source["image"], "digest": source["digest"]}

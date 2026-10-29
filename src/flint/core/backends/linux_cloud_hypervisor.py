"""Cloud-Hypervisor backend: jailed cloud-hypervisor VMs (see :mod:`flint.core.vmm.cloud_hypervisor`).

Uses the same bridge, per-VM network namespaces and guest image as the
Firecracker backend. CH has no jailer of its own: Flint's
:class:`~flint.core.vmm.cloud_hypervisor.jail.CloudHypervisorJail` gives it an
equivalent chroot, identity and cgroup. It can also boot VMs on a cluster's
shared root disks and migration network, which lets them move live.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping

from .. import config
from ..config import DEFAULT_TEMPLATE_ID, log
from ..net.netns import _delete_netns
from ..types import BootResult, LivePhase, Maintenance, SandboxRow, SandboxState
from ..vmm.cloud_hypervisor import api as ch_api
from ..vmm.cloud_hypervisor import boot as ch
from ..vmm.cloud_hypervisor.jail import JAIL_ROOTFS, CloudHypervisorJail, jail_dir
from .linux import LinuxVmmBackend


class LinuxCloudHypervisorBackend(LinuxVmmBackend):
    name = "cloud-hypervisor"
    kind = "linux-cloud-hypervisor"
    display_name = "Cloud-Hypervisor (Linux)"
    supports_live_migration = True
    ns_prefix = ch.NS_PREFIX
    jail_type = CloudHypervisorJail
    vmm_disk_limit = True

    tap_for_ns = staticmethod(ch.tap_for_ns)

    def _vmm_preflight(self) -> list[str]:
        problems = []
        if not os.path.exists(config.CH_BINARY) and not shutil.which("cloud-hypervisor"):
            problems.append(f"cloud-hypervisor binary not found at {config.CH_BINARY} or on PATH")
        if not os.path.exists(config.SOURCE_ROOTFS):
            problems.append(f"source rootfs not found at {config.SOURCE_ROOTFS}")
        for tool in ("setpriv", "chroot"):
            if not shutil.which(tool):
                problems.append(f"{tool} not found on PATH (needed to jail cloud-hypervisor)")
        return problems

    def vmm_binary(self) -> str:
        return config.CH_BINARY

    def vm_resources(self) -> tuple[int, int]:
        return config.CH_CPU_COUNT, config.CH_MEMORY_BYTES >> 20

    def template_artifact_valid(self, template_dir: str) -> bool:
        # A rootfs is enough: without a golden snapshot, create() cold-boots.
        # Per-sandbox pause snapshots live under each sandbox's vm_dir.
        return os.path.exists(os.path.join(template_dir, ch.ROOTFS))

    def install_dependencies(self, **kwargs) -> None:
        from .._install import KERNEL_RELEASE, _detect_arch, _install_kernel, install_cloud_hypervisor

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

    def ensure_default_template(self) -> None:
        from .._template_registry import register_template_artifact

        golden_dir = config.CH_GOLDEN_DIR
        os.makedirs(golden_dir, exist_ok=True)
        status = "ready"
        try:
            ch.create_golden(dest_dir=golden_dir, source_rootfs=config.SOURCE_ROOTFS)
        except Exception as exc:
            if ch.golden_ready(golden_dir):
                # Sandboxes still work, they just cold-boot instead of restoring.
                log.warning("ch golden snapshot not created, falling back to fresh boot: %s", exc)
            else:
                log.warning("ch golden template not installed: %s", exc)
                status = "pending"
        register_template_artifact(DEFAULT_TEMPLATE_ID, "Default (Alpine)", self.kind, golden_dir, status=status)
        log.info("cloud-hypervisor default template registered (status=%s, dir=%s)", status, golden_dir)

    def _pool_dir(self) -> str:
        return config.CH_POOL_DIR

    def _pool_sources(self, template_id: str) -> dict[str, str] | None:
        try:
            template_dir = ch.template_dir(template_id)
        except KeyError:
            return None
        if not ch.snapshot_ready(template_dir):
            return None
        return {ch.ROOTFS: os.path.join(ch.snapshot_dir(template_dir), ch.ROOTFS)}

    def _require_template(self, template_id: str) -> None:
        if template_id == DEFAULT_TEMPLATE_ID and not ch.golden_ready():
            raise RuntimeError(f"cloud-hypervisor golden rootfs not found in {config.CH_GOLDEN_DIR}")

    def _boot(self, **kwargs) -> BootResult:
        return ch.boot(**kwargs)

    def _shutdown_vmm(self, entry) -> None:
        ch.shutdown(entry.socket_path)

    def _snapshot_and_hold(self, entry) -> str:
        return ch.pause(entry.vm_id)

    def resume(self, row: Mapping) -> BootResult:
        row = SandboxRow.of(row)
        meta = row.backend_metadata
        ns_name = self._row_ns_name(row)
        return ch.resume(
            row.vm_id,
            ns_name=ns_name,
            tap_name=meta.get("tap_name") or ch.tap_for_ns(ns_name),
            veth_ip=meta.get("veth_ip", ""),
            maintenance=Maintenance.of(meta),
        )

    # ── Recovery ──────────────────────────────────────────────────────────

    def _row_ns_name(self, row: SandboxRow) -> str:
        return row.ns_name or ch.ns_name_for(row.vm_id)

    def _vmm_held_paused(self, row: SandboxRow) -> bool:
        # A live migration's source after the send: the control plane decides
        # whether it is rolled back (resumed) or deleted, so keep its VMM.
        return row.state == SandboxState.PAUSED and row.maintenance.live_phase == LivePhase.SENT

    def _discard_jail(self, row: SandboxRow) -> None:
        super()._discard_jail(row)
        # Sandboxes created before CH was jailed kept their files in the data dir.
        if row.vm_dir and not row.vm_dir.startswith(jail_dir()):
            from .._wipe import wipe_tree
            wipe_tree(row.vm_dir, row.vm_id)

    def _check_recovered(self, row: SandboxRow) -> None:
        import json
        from .._limits import DISK_BPS
        rootfs = os.path.join(row.vm_dir, ch.ROOTFS)
        if os.major(os.stat(rootfs).st_dev):
            return  # a local disk: the cgroup bounds its IO
        # A shared disk isn't a block device cgroups can limit; CH's own limiter must be on.
        status, body = ch_api.ch_get(row.socket_path, "/api/v1/vm.info")
        disks = json.loads(body).get("config", {}).get("disks", []) if status == 200 else []
        if not disks or any(
            (disk.get("rate_limiter_config") or {}).get("bandwidth", {}).get("size") != DISK_BPS
            or (disk.get("rate_limiter_config") or {}).get("bandwidth", {}).get("refill_time") != 1000
            for disk in disks
        ):
            raise RuntimeError("Recovered shared-disk VM needs a bounded disk configuration; restore from its snapshot")

    # ── Cluster hooks ──────────────────────────────────────────────────────

    def pause_files(self, row: Mapping) -> tuple[str, dict[str, str]]:
        files = {ch.ROOTFS: ch.ROOTFS}
        files.update({f"pause/{n}": f"pause/{n}" for n in ch.SNAPSHOT_FILES})
        return CloudHypervisorJail(row["vm_id"]).root, files

    def checkpoint(self, entry, snapshot):
        from .._wipe import wipe_tree

        jail = CloudHypervisorJail(entry.vm_id)
        staging = jail.host_path("/ckpt")
        shutil.rmtree(staging, ignore_errors=True)
        os.makedirs(staging)
        jail.give("/ckpt")
        ch_api.vm_pause(jail.socket_path)
        try:
            ch_api.vm_snapshot(jail.socket_path, "file:///ckpt")
            files = {ch.ROOTFS: ch.ROOTFS}
            files.update({f"pause/{n}": f"ckpt/{n}" for n in ch.SNAPSHOT_FILES})
            return snapshot(jail.root, files)
        finally:
            try:
                ch_api.vm_resume(jail.socket_path)
            finally:
                wipe_tree(staging, entry.vm_id)  # a copy of the VM's memory

    def import_snapshot(self, vm_id: str, src_dir: str, *, allow_internet_access: bool, use_pool: bool = True) -> BootResult:
        from .._shared_rootfs import configured_pool
        from ..net.netns_pool import acquire_netns

        shared = configured_pool()
        maintenance = Maintenance()
        jail = CloudHypervisorJail(vm_id)
        jail.cleanup()  # a stale jail from an earlier life of this ID
        jail.prepare()
        ns_name = ""
        try:
            if shared:
                ch.attach_shared_disk(jail, shared, os.path.join(src_dir, ch.ROOTFS), maintenance, verify=True)
            else:
                os.rename(os.path.join(src_dir, ch.ROOTFS), jail.host_path(JAIL_ROOTFS))
            jail.give(JAIL_ROOTFS)
            os.mkdir(jail.host_path("/pause"))
            for n in ch.SNAPSHOT_FILES:
                os.rename(os.path.join(src_dir, "pause", n), jail.host_path(f"/pause/{n}"))
                jail.give(f"/pause/{n}")
            jail.give("/pause")
            veth_ip = ""
            if shared:
                network = ch.migration_network(vm_id, maintenance)
                ns_name, tap_name = ch.setup_migration_network(network, vm_id, maintenance, allow_internet_access)
            else:
                net = acquire_netns(ch.NS_PREFIX, ch.tap_for_ns, allow_internet_access, use_pool=use_pool)
                ns_name, tap_name, veth_ip = net.ns_name, net.tap_name, net.veth_ip
            return ch.resume(
                vm_id, ns_name=ns_name, tap_name=tap_name,
                allow_internet_access=allow_internet_access, veth_ip=veth_ip, maintenance=maintenance,
            )
        except Exception:
            if ns_name:
                _delete_netns(ns_name)
            jail.cleanup()
            raise

    def template_files(self, template_dir: str) -> list[str]:
        return [ch.ROOTFS] + [f"snapshot/{n}" for n in (*ch.SNAPSHOT_FILES, ch.ROOTFS)]

    def _build_template_in_job(self, name: str, dockerfile: str, rootfs_size_mb: int) -> dict:
        """Build a CH template: Docker build -> rootfs extraction -> golden snapshot -> register."""
        import tempfile

        from .._build_job import output_dir
        from .._builder import build_rootfs
        from .._template_build import _build_flintd, _find_init_net_sh, _slugify
        from .._template_registry import register_template_artifact, update_template_artifact_status
        from .._wipe import wipe_tree

        template_id = _slugify(name)
        template_dir = output_dir(template_id)
        os.makedirs(template_dir, exist_ok=True)

        # A supervised build stages its output; the supervisor records the
        # published directory, so keep the current one bootable meanwhile.
        if os.environ.get("FLINT_BUILD_JOB"):
            update_template_artifact_status(template_id, self.kind, "building")
        else:
            register_template_artifact(template_id, name, self.kind, template_dir,
                                       status="building", rootfs_size_mb=rootfs_size_mb)

        context_dir = tempfile.mkdtemp(prefix="context-", dir=template_dir)
        try:
            _build_flintd(os.path.join(context_dir, "flintd"))
            shutil.copy2(_find_init_net_sh(), os.path.join(context_dir, "init-net.sh"))
            with open(f"{template_dir}/Dockerfile", "w") as f:
                f.write(dockerfile)
            build_rootfs(self.kind, dockerfile, context_dir, f"{template_dir}/{ch.ROOTFS}", rootfs_size_mb)
            ch.create_golden_snapshot(template_dir)

            # The supervisor publishes only after worker cleanup and durable output.
            if not os.environ.get("FLINT_BUILD_JOB"):
                update_template_artifact_status(template_id, self.kind, "ready")
            log.info("CH template %s built successfully", template_id)
            return {"template_id": template_id, "status": "ready"}
        except Exception:
            update_template_artifact_status(template_id, self.kind, "failed")
            raise
        finally:
            wipe_tree(context_dir)


from .registry import register as _register

_register(LinuxCloudHypervisorBackend)

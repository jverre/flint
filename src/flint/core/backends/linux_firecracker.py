"""Firecracker backend: jailed Firecracker VMs restored from template snapshots."""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping

from .. import config
from ..config import DEFAULT_TEMPLATE_ID, GOLDEN_DIR
from ..net.netns import _delete_netns
from ..types import BootResult, SandboxRow
from ..vmm.firecracker import boot as fc
from ..vmm.firecracker.golden import create_golden_snapshot, golden_snapshot_exists
from ..vmm.firecracker.jail import FirecrackerJail
from .linux import LinuxVmmBackend


class LinuxFirecrackerBackend(LinuxVmmBackend):
    name = "firecracker"
    kind = "linux-firecracker"
    display_name = "Firecracker (Linux)"
    ns_prefix = fc.NS_PREFIX
    jail_type = FirecrackerJail

    tap_for_ns = staticmethod(fc.tap_for_ns)

    def _vmm_preflight(self) -> list[str]:
        problems = []
        if not os.path.exists(config.FIRECRACKER_BINARY):
            problems.append(f"firecracker binary not found at {config.FIRECRACKER_BINARY}")
        if not shutil.which(config.JAILER_BINARY) and not os.path.exists(config.JAILER_BINARY):
            problems.append(f"jailer binary not found (searched PATH and {config.JAILER_BINARY})")
        return problems

    def vmm_binary(self) -> str:
        return config.FIRECRACKER_BINARY

    def template_artifact_valid(self, template_dir: str) -> bool:
        return all(os.path.exists(f"{template_dir}/{f}") for f in fc.TEMPLATE_FILES)

    def install_dependencies(self, **kwargs) -> None:
        from .._install import KERNEL_RELEASE, install_deps as _install_deps

        _install_deps(
            fc_version=kwargs.get("version", "latest"),
            install_dir=kwargs.get("install_dir", "/usr/local/bin"),
            kernel_dir=kwargs.get("kernel_dir", "/root/firecracker-vm"),
            kernel_release=kwargs.get("kernel_release") or KERNEL_RELEASE,
            skip_kernel=kwargs.get("skip_kernel", False),
        )

    def _ensure_vmm_ready(self) -> None:
        from .._artifacts import prune_shared
        prune_shared(config.JAILER_SHARED_DIR)

    def ensure_default_template(self) -> None:
        from .._artifacts import golden_inputs_current, record_golden_inputs
        from .._template_registry import register_template_artifact

        if not golden_snapshot_exists() or not golden_inputs_current(GOLDEN_DIR):
            shutil.rmtree(GOLDEN_DIR, ignore_errors=True)
            create_golden_snapshot()
            record_golden_inputs(GOLDEN_DIR)
        register_template_artifact(DEFAULT_TEMPLATE_ID, "Default (Alpine)", self.kind, GOLDEN_DIR, status="ready")

    def _pool_dir(self) -> str:
        return config.FC_POOL_DIR

    def _pool_sources(self, template_id: str) -> dict[str, str] | None:
        if not self._template_ready(template_id):
            return None
        snapshot_dir = fc.template_snapshot_dir(template_id)
        return {fc.ROOTFS: f"{snapshot_dir}/{fc.ROOTFS}", fc.PAUSE_MEM: f"{snapshot_dir}/mem"}

    def _template_ready(self, template_id: str) -> bool:
        if template_id == DEFAULT_TEMPLATE_ID:
            return golden_snapshot_exists()
        from .._template_registry import template_snapshot_exists
        return template_snapshot_exists(template_id, backend_kind=self.kind)

    def _require_template(self, template_id: str) -> None:
        if self._template_ready(template_id):
            return
        if template_id == DEFAULT_TEMPLATE_ID:
            raise RuntimeError(f"Golden snapshot not found in {GOLDEN_DIR}")
        raise RuntimeError(f"Template snapshot not found: {template_id}")

    def _boot(self, **kwargs) -> BootResult:
        return fc.boot(**kwargs)

    def _snapshot_and_hold(self, entry) -> str:
        return fc.pause(entry.vm_id)

    def resume(self, row: Mapping) -> BootResult:
        return fc.resume(SandboxRow.of(row))

    def _discard_jail(self, row: SandboxRow) -> None:
        if row.chroot_base:  # rows from before the jailer have no jail
            super()._discard_jail(row)

    # ── Cluster hooks ──────────────────────────────────────────────────────

    def pause_files(self, row: Mapping) -> tuple[str, dict[str, str]]:
        return FirecrackerJail(row["vm_id"]).root, {name: name for name in fc.PAUSE_FILES}

    def checkpoint(self, entry, snapshot):
        # A diff snapshot writes the pages dirtied since the last one into
        # pause-mem, which then holds the full memory image, exactly like a
        # pause; the VM keeps running from its current backing afterwards.
        jail = FirecrackerJail(entry.vm_id)
        with fc.guest_paused(jail.socket_path):
            fc.write_pause_snapshot(jail)
            return snapshot(jail.root, {name: name for name in fc.PAUSE_FILES})

    def import_snapshot(self, vm_id: str, src_dir: str, *, allow_internet_access: bool, use_pool: bool = True) -> BootResult:
        from ..net.netns_pool import acquire_netns

        jail = FirecrackerJail(vm_id)
        jail.cleanup()  # a stale jail from an earlier life of this ID
        jail.prepare()
        net = None
        try:
            for name in fc.PAUSE_FILES:
                os.rename(os.path.join(src_dir, name), jail.host_path("/" + name))
                jail.give("/" + name)
            net = acquire_netns(fc.NS_PREFIX, fc.tap_for_ns, allow_internet_access, use_pool=use_pool)
            return self.resume(SandboxRow(
                vm_id=vm_id, vm_dir=jail.root, ns_name=net.ns_name, chroot_base=jail.base,
                backend_metadata={"veth_ip": net.veth_ip},
            ))
        except Exception:
            if net is not None:
                _delete_netns(net.ns_name)
            jail.cleanup()
            raise

    def template_files(self, template_dir: str) -> list[str]:
        return list(fc.TEMPLATE_FILES)

    def _build_template_in_job(self, name: str, dockerfile: str, rootfs_size_mb: int) -> dict:
        from .._template_build import build_template
        template_id = build_template(name, dockerfile, rootfs_size_mb=rootfs_size_mb)
        return {"template_id": template_id, "status": "building"}


from .registry import register as _register

_register(LinuxFirecrackerBackend)

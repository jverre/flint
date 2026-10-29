"""What the Linux backends share: jailed VMMs on per-VM network namespaces.

Firecracker and Cloud Hypervisor sandboxes are set up the same way: each
VMM runs in a :class:`~flint.core.vmm.jail.Jail` (chroot, identity, cgroup)
inside its own network namespace on the host bridge, and the guest agent is
reached through that namespace. :class:`LinuxVmmBackend` implements the
lifecycle around that once — pooling, killing, pausing, recovery after a
restart, confinement — and each hypervisor supplies only what differs:
how a VM boots, resumes and snapshots, and its jail.
"""

from __future__ import annotations

import os
import shutil
import socket
from abc import abstractmethod
from collections.abc import Mapping
from typing import ClassVar

from .. import config
from ..config import GUEST_IP, log
from ..net.netns import _delete_netns, _ensure_bridge, _enter_netns, _restore_netns, ensure_host_firewall, reserve_existing_veth_ips
from ..net.netns_pool import start_netns_pool, stop_netns_pool
from ..types import BootResult, SandboxRow, SandboxState, _SandboxEntry
from ..vmm.common import AGENT_URL, RecoveredProcess, stop_process, wait_for_agent
from ..vmm.jail import Jail
from .base import BackendPlugin, RecoveryState


def _running(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class LinuxVmmBackend(BackendPlugin):
    supported_platforms = ("linux",)
    supports_tenant_networks = True
    supports_volume_mounts = True
    supports_confined_api = True

    # Prefix of this backend's network namespaces (the netns pool's and recovery's key).
    ns_prefix: ClassVar[str]
    jail_type: ClassVar[type[Jail]]
    # cgroup io.max also bounds the VMM's own disk IO (it has no rate limiter of its own).
    vmm_disk_limit: ClassVar[bool] = False

    # ── What each hypervisor supplies ─────────────────────────────────────

    @staticmethod
    @abstractmethod
    def tap_for_ns(ns_name: str) -> str:
        """The guest's TAP device in namespace ``ns_name``."""

    @abstractmethod
    def _pool_dir(self) -> str:
        """Where pre-copied VM disks wait for a create."""

    @abstractmethod
    def _pool_sources(self, template_id: str) -> dict[str, str] | None:
        """Files the disk pool pre-copies for ``template_id`` (name -> source), or None if it can't."""

    @abstractmethod
    def _require_template(self, template_id: str) -> None:
        """Raise if ``template_id`` can't be booted on this host."""

    @abstractmethod
    def _boot(self, **kwargs) -> BootResult:
        """Start a VM (``create``'s arguments)."""

    @abstractmethod
    def _snapshot_and_hold(self, entry) -> str:
        """Pause the guest and write its state into its jail; the VMM stays up. Returns the state's path."""

    @abstractmethod
    def _vmm_preflight(self) -> list[str]:
        """Problems with the hypervisor's own binaries and images."""

    def _shutdown_vmm(self, entry) -> None:
        """Ask the VMM to exit cleanly before it is killed."""

    def _row_ns_name(self, row: SandboxRow) -> str:
        return row.ns_name

    def _vmm_held_paused(self, row: SandboxRow) -> bool:
        """A paused row whose VMM must still be running (and adopted as such)."""
        return False

    def _discard_jail(self, row: SandboxRow) -> None:
        self.jail_type(row.vm_id).cleanup()

    def _check_recovered(self, row: SandboxRow) -> None:
        """Raise if an adopted VM's VMM isn't configured as this daemon would have."""

    def _ensure_vmm_ready(self) -> None:
        """Host setup the hypervisor needs beyond the shared bridge and firewall."""

    # ── Host ──────────────────────────────────────────────────────────────

    def preflight(self) -> list[str]:
        problems = super().preflight()
        if problems:
            return problems
        problems += self._vmm_preflight()
        if not os.path.exists(config.KERNEL_PATH):
            problems.append(f"kernel image not found at {config.KERNEL_PATH}")
        for tool in ("nft", "tc", "bwrap", "setpriv"):
            if not shutil.which(tool):
                problems.append(f"{tool} is required for workload isolation")
        return problems

    def ensure_runtime_ready(self) -> None:
        _ensure_bridge()
        ensure_host_firewall()
        reserve_existing_veth_ips()
        self._ensure_vmm_ready()

    def start_pool(self) -> None:
        from .._pool import start_pool
        start_pool(self._pool_dir(), self._pool_sources)
        start_netns_pool(self.ns_prefix, self.tap_for_ns)

    def stop_pool(self) -> None:
        from .._pool import stop_pool
        stop_pool()
        stop_netns_pool()

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def create(
        self,
        *,
        template_id: str,
        allow_internet_access: bool,
        use_pool: bool,
        use_pyroute2: bool,
        vm_id: str | None = None,
    ) -> BootResult:
        self._require_template(template_id)
        return self._boot(
            template_id=template_id,
            allow_internet_access=allow_internet_access,
            use_pool=use_pool,
            use_pyroute2=use_pyroute2,
            vm_id=vm_id,
        )

    def kill(self, entry) -> None:
        self._shutdown_vmm(entry)
        stop_process(entry.process)
        _delete_netns(entry.ns_name)
        self.jail_type(entry.vm_id).cleanup()

    def pause(self, entry) -> str:
        ref = self._snapshot_and_hold(entry)
        # A restore needs a fresh VMM, so this one's job is done.
        stop_process(entry.process)
        return ref

    def discard_paused(self, row: Mapping) -> None:
        # Its VMM exited at pause; the recorded pid may belong to something else now.
        self._cleanup_dead_row(SandboxRow({**row, "pid": 0}))

    def check_entry_alive(self, entry) -> tuple[bool, str | None]:
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

    def connect_guest(self, entry) -> socket.socket:
        # A socket belongs to the netns it was created in.
        orig_fd = _enter_netns(entry.ns_name)
        try:
            return super().connect_guest(entry)
        finally:
            _restore_netns(orig_fd)

    # ── Recovery ──────────────────────────────────────────────────────────

    def recover_row(self, row: Mapping) -> tuple[RecoveryState, _SandboxEntry | None]:
        row = SandboxRow.of(row)
        held = self._vmm_held_paused(row)
        if row.state == SandboxState.PAUSED and not held:
            return "paused", None
        if not _running(row.pid):
            self._cleanup_dead_row(row)
            return "dead", None

        ns_name = self._row_ns_name(row)
        try:
            # A held VMM's guest is paused: it can't answer.
            agent_url = AGENT_URL if held else wait_for_agent(ns_name, retries=50)
        except Exception:
            self._cleanup_dead_row(row)
            return "dead", None

        entry = _SandboxEntry.from_row(
            row, kind=self.kind, process=RecoveredProcess(row.pid), agent_url=agent_url, guest_ip=GUEST_IP,
            state=SandboxState.PAUSED if held else SandboxState.RUNNING, ns_name=ns_name,
        )
        return "alive", entry

    def _cleanup_dead_row(self, row: SandboxRow) -> None:
        if row.pid:
            try:
                os.kill(row.pid, 9)
            except (ProcessLookupError, PermissionError):
                pass
        try:
            _delete_netns(self._row_ns_name(row))
        except Exception:
            log.exception("[%s] deleting the network namespace of a dead VM failed", row.vm_id[:8])
        self._discard_jail(row)

    # ── Confinement ───────────────────────────────────────────────────────

    def vm_cgroup(self, vm_id: str) -> str:
        return self.jail_type(vm_id).cgroup

    def force_stop(self, vm_id: str, *, pid: int, root: str, process=None) -> bool:
        from .._limits import stop_processes
        stop_processes(self.vm_cgroup(vm_id), pid=pid, root=root, process=process)
        return True

    def prepare_recovery(self, rows: list[Mapping]) -> None:
        """Refuse VMs of an older identity scheme, clear what a crashed daemon
        left (credential proxies, template builds), and move legacy processes
        out of delegated parents before enabling controllers."""
        from .._build_job import recover_builds
        from .._builder import cleanup_abandoned_builders
        from .._identities import vm_identity
        from .._limits import attach_process, owns_process
        from ..net.proxy import cleanup_abandoned_proxies
        rows = [SandboxRow.of(row) for row in rows]
        for row in rows:
            if row.state not in (SandboxState.RUNNING, SandboxState.PAUSED) or not os.path.isdir(row.vm_dir):
                continue
            owner = os.stat(row.vm_dir, follow_symlinks=False)
            if (owner.st_uid, owner.st_gid) != vm_identity(row.vm_id):
                raise RuntimeError(
                    f"VM {row.vm_id} uses a legacy host identity. Finish, export or delete "
                    "existing VMs with the previous Flint release before upgrading this worker."
                )
        cleanup_abandoned_proxies()
        recover_builds()
        cleanup_abandoned_builders()
        for row in rows:
            if owns_process(row.pid, row.vm_dir):
                attach_process(self.vm_cgroup(row.vm_id), row.pid)

    def reconcile_recovered(self, row: Mapping) -> None:
        """Repair a surviving VM's namespace (firewall, proxy redirect), which a restart may have lost."""
        from ..net.netns import reconcile_namespace
        row = SandboxRow.of(row)
        reconcile_namespace(row.ns_name, proxy=bool(row.get("network_policy_json")))

    def confine_recovered(self, row: Mapping) -> None:
        from .._limits import attach_process, cgroup_limits, owns_process
        row = SandboxRow.of(row)
        if not owns_process(row.pid, row.vm_dir):
            raise RuntimeError("Recovered process identity does not match its jail")
        self._check_recovered(row)
        cpus, memory = self.vm_resources()
        group = self.vm_cgroup(row.vm_id)
        cgroup_limits(group, os.path.join(row.vm_dir, "rootfs.ext4"), vcpus=cpus, memory_mib=memory,
                      vmm_disk_limit=self.vmm_disk_limit)
        attach_process(group, row.pid)

    # ── Templates ─────────────────────────────────────────────────────────

    def build_template(self, name: str, dockerfile: str, rootfs_size_mb: int = 500) -> dict:
        # Builds run in a supervised job process; this is called again inside it.
        if not os.environ.get("FLINT_BUILD_JOB"):
            from .._build_job import run_build
            return run_build(self.kind, name, dockerfile, rootfs_size_mb)
        return self._build_template_in_job(name, dockerfile, rootfs_size_mb)

    @abstractmethod
    def _build_template_in_job(self, name: str, dockerfile: str, rootfs_size_mb: int) -> dict:
        pass

    def delete_template_artifact(self, template_id: str, template: dict | None = None) -> None:
        from .._template_registry import delete_template_artifact
        delete_template_artifact(template_id, self.kind)

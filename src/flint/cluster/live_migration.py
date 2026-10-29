"""Cluster live migration lifecycle, with a fenced receiver before cutover.

Each side records where the migration stands as the VM's
:class:`~flint.core.types.Maintenance` ``live_phase`` (persisted through the
sandbox manager), so a restart or the control plane can tell what happened.
"""

from __future__ import annotations

import json
import os
import threading
import time

from flint.core import config
from flint.core.vmm.cloud_hypervisor import api as ch
from flint.core.vmm.cloud_hypervisor.jail import JAIL_ROOTFS, CloudHypervisorJail
from flint.core.net.migration_network import configured_network
from flint.core._shared_rootfs import configured_pool
from flint.core.types import BootResult, LivePhase, Maintenance, SandboxRow, SandboxState, _SandboxEntry
from .gateway import gateway_request
from .migration_transport import MigrationRelay


class LiveMigration:
    def __init__(self, agent):
        self.agent = agent
        self.daemon = agent.daemon
        self.relays: dict[str, MigrationRelay] = {}
        self.workers: dict[str, threading.Thread] = {}
        self.lock = threading.Lock()

    def _record(self, entry, phase: LivePhase | None = None, *, state: SandboxState | None = None, **changes) -> Maintenance:
        """Update the VM's maintenance state (``live_phase`` and ``changes``) and persist it."""
        maintenance = Maintenance.of(entry.backend_metadata)
        if phase is not None:
            maintenance.live_phase = phase
        for name, value in changes.items():
            setattr(maintenance, name, value)
        maintenance.save(entry.backend_metadata)
        self.daemon.manager.save(entry, state=state)
        return maintenance

    @staticmethod
    def _version(socket_path):
        code, raw = ch.ch_get(socket_path, "/api/v1/vmm.ping")
        if code != 200:
            raise RuntimeError("VMM version unavailable")
        return json.loads(raw)["version"]

    def prepare(self, vm_id: str, body: dict) -> dict:
        if not self.daemon.backend.supports_live_migration:
            raise ValueError("live migration requires Cloud Hypervisor")
        pool = configured_pool()
        if not pool or pool.pool_id != body["pool_id"]:
            raise ValueError(
                "source and destination must use the same shared root disk pool"
            )
        disk = pool.descriptor(vm_id)
        if disk["vni"] != body["vni"]:
            raise ValueError("migration network identity differs")
        net = configured_network(vm_id, disk["vni"])
        jail = CloudHypervisorJail(vm_id)
        mgr = self.daemon.manager
        with self.lock:
            if mgr.get_entry(vm_id) or os.path.exists(jail.base):
                raise ValueError("destination already has this VM or a stale jail")
            relay = MigrationRelay()
            process = None
            namespace = ""
            try:
                jail.prepare()
                with open(jail.shared_disk_marker, "x") as marker:
                    marker.write(pool.pool_id)
                disk = pool.attach(
                    vm_id, jail.host_path(JAIL_ROOTFS), expected=body["root_disk"]
                )
                gateway_request("prepare", vm_id, internet=body["internet"])
                namespace, tap = net.setup_compute(body["internet"])
                process = jail.spawn(namespace)
                maintenance = Maintenance(
                    root_disk=disk,
                    network=body["network"],
                    live_phase=LivePhase.RECEIVING,
                    original_availability=body["availability"],
                )
                receiver = BootResult(
                    vm_id=vm_id,
                    process=process,
                    vm_dir=jail.root,
                    agent_url=f"http://{config.GUEST_IP}:{config.AGENT_PORT}",
                    guest_ip=config.GUEST_IP,
                    ns_name=namespace,
                    socket_path=jail.socket_path,
                    chroot_base=jail.base,
                    metadata={
                        "tap_name": tap,
                        "veth_ip": "",
                        "resources": body["resources"],
                        "maintenance": maintenance.to_json(),
                    },
                )
                entry = _SandboxEntry.from_boot(
                    receiver,
                    kind=self.daemon.backend.kind,
                    template_id=body["template_id"],
                    tenant=body["tenant"],
                    # Even ephemeral receivers are fenced until the CP commits.
                    availability=body["availability"]
                    if body["availability"] in ("durable", "restartable")
                    else "restartable",
                    state=SandboxState.STARTING,
                )
                mgr.adopt(entry)
                self.agent.fence_started(vm_id)
                version = self._version(jail.socket_path)
                port = relay.receiver(
                    net.local_ip,
                    jail.host_path("/migration-receive.sock"),
                    body["source_host"],
                )
                self.relays[vm_id] = relay

                def receive():
                    try:
                        ch.vm_receive_migration(
                            jail.socket_path, "unix:/migration-receive.sock"
                        )
                        # CH has already resumed the guest. Persist this irreversible
                        # boundary before routing work that can still fail.
                        self._record(entry, LivePhase.CUTOVER, live_executed=True)
                        gateway_request("activate", vm_id)
                        net.announce()
                        entry.agent_healthy = True
                        self._record(entry, LivePhase.RECEIVED, state=SandboxState.RUNNING)
                    except Exception as exc:
                        executed = Maintenance.of(entry.backend_metadata).live_executed
                        self._record(entry, LivePhase.CUTOVER_FAILED if executed else None, live_error=str(exc))
                        # An uncertain receiver must never be left running.
                        mgr.kill(vm_id)
                    finally:
                        relay.close()
                        if self.relays.get(vm_id) is relay:
                            self.relays.pop(vm_id, None)

                worker = threading.Thread(
                    target=receive, daemon=True, name="live-migration-receive"
                )
                self.workers[vm_id] = worker
                worker.start()
                return {"address": net.local_ip, "port": port, "vmm_version": version}
            except Exception:
                relay.close()
                if process:
                    process.kill()
                    process.wait(timeout=5)
                if namespace:
                    from flint.core.net.netns import _delete_netns

                    _delete_netns(namespace)
                jail.cleanup()
                mgr.untrack(vm_id)
                raise

    def send(self, vm_id: str, body: dict) -> dict:
        entry = self.daemon.manager.get_entry(vm_id)
        if not entry or entry.state != SandboxState.RUNNING:
            raise ValueError("source VM is not running")
        if entry.network_policy:
            raise ValueError("credential proxy connections cannot migrate")
        source_version = self._version(entry.socket_path).lstrip("v")
        target_version = str(body["vmm_version"]).lstrip("v")
        pairs = set(os.environ.get("FLINT_CH_MIGRATION_ALLOWED_PAIRS", "").split(","))
        if (
            source_version != target_version
            and f"{source_version}>{target_version}" not in pairs
        ):
            raise ValueError(
                "running VMM upgrade pair has not been validated and allowed"
            )
        if not Maintenance.of(entry.backend_metadata).migratable:
            raise ValueError("VM was not booted with migration storage and networking")
        relay = MigrationRelay()
        path = os.path.join(entry.vm_dir, "migration-send.sock")
        self._record(entry, LivePhase.SENDING)
        self.relays[vm_id] = relay
        t0 = time.monotonic()
        try:
            relay.sender(path, body["address"], body["port"], body["target_host"])
            ch.vm_send_migration(entry.socket_path, "unix:/migration-send.sock")
            self._record(entry, LivePhase.SENT, state=SandboxState.PAUSED)
            return {"transferred": True, "total_ms": (time.monotonic() - t0) * 1000}
        except Exception:
            self._record(entry, LivePhase.SEND_FAILED)
            raise
        finally:
            relay.close()
            if self.relays.get(vm_id) is relay:
                self.relays.pop(vm_id, None)
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    def status(self, vm_id: str) -> dict:
        entry = self.daemon.manager.get_entry(vm_id)
        if not entry:
            row = self.daemon._state_store.get_sandbox(vm_id)
            if row and self._alive(row["pid"]):
                return {"phase": "unknown", "vmm_state": "Unknown"}
            return {"phase": "absent", "vmm_state": "Absent"}
        phase = Maintenance.of(entry.backend_metadata).live_phase
        state = "Unknown"
        if phase in (LivePhase.RECEIVING, LivePhase.CUTOVER):
            # The VMM is busy receiving: asking it would block.
            return {
                "phase": phase.value,
                "vmm_state": state,
                "vm": entry.to_dict(),
            }
        try:
            code, raw = ch.ch_get(entry.socket_path, "/api/v1/vm.info")
            if code == 200:
                state = json.loads(raw)["state"]
        except OSError:
            try:
                os.kill(entry.pid, 0)
            except ProcessLookupError:
                state = "Absent"
        return {
            "phase": phase.value if phase else "none",
            "vmm_state": state,
            "vm": entry.to_dict(),
        }

    @staticmethod
    def _alive(pid: int) -> bool:
        if pid <= 1:
            return False
        try:
            # Zombies have stopped executing and cannot write the disk.
            with open(f"/proc/{pid}/stat") as stream:
                return stream.read().rsplit(")", 1)[1].split()[0] != "Z"
        except FileNotFoundError:
            return False

    def abort(self, vm_id: str) -> dict:
        with self.lock:
            relay = self.relays.get(vm_id)
            if relay:
                relay.close()
            entry = self.daemon.manager.get_entry(vm_id)
            row = self.daemon._state_store.get_sandbox(vm_id)
            pid = entry.pid if entry else (row or {}).get("pid", 0)
            maintenance = Maintenance.of(
                entry.backend_metadata if entry else SandboxRow.of(row or {}).backend_metadata
            )
            executed = maintenance.live_executed or maintenance.live_phase in (
                LivePhase.RECEIVED,
                LivePhase.CUTOVER_FAILED,
            )
            self.daemon.kill_vm(vm_id)
            worker = self.workers.get(vm_id)
            if worker:
                worker.join(timeout=5)
                if worker.is_alive():
                    raise RuntimeError("migration receiver has not stopped")
            if self._alive(pid):
                raise RuntimeError("receiver process death is unconfirmed")
            if not row and os.path.exists(CloudHypervisorJail(vm_id).base):
                raise RuntimeError("untracked destination jail requires reconciliation")
            return {"phase": "absent", "executed": executed}

    def rollback(self, vm_id: str) -> dict:
        # Only the control plane may call this, after confirming receiver deletion.
        entry = self.daemon.manager.get_entry(vm_id)
        if not entry:
            raise ValueError("source VMM no longer exists")
        status, raw = ch.ch_get(entry.socket_path, "/api/v1/vm.info")
        if status != 200:
            raise ValueError("cannot establish source VMM state")
        state = json.loads(raw)["state"]
        if state != "Running":
            # The receiver may have written the shared disk after cutover. An
            # old paused memory image must never resume against those writes.
            raise ValueError(
                "source did not resume itself; recover from a coherent checkpoint"
            )
        self._record(entry, LivePhase.ROLLED_BACK, state=SandboxState.RUNNING)
        self.agent.fence_started(vm_id)
        return {"vm": entry.to_dict()}

    def commit(self, vm_id: str) -> dict:
        entry = self.daemon.manager.get_entry(vm_id)
        maintenance = Maintenance.of(entry.backend_metadata) if entry else None
        if not maintenance or maintenance.live_phase != LivePhase.RECEIVED:
            raise ValueError("receiver has not completed")
        entry.availability = maintenance.original_availability
        self.daemon.manager.save(entry)
        self.agent._arm_fence()
        return {"vm": entry.to_dict()}

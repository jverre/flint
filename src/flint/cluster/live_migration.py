"""Cluster live migration lifecycle, with a fenced receiver before cutover."""

from __future__ import annotations

import json
import os
import threading
import time

from flint.core import config, _cloud_hypervisor as ch
from flint.core._ch_jail import ChJail, JAIL_ROOTFS
from flint.core._migration_network import configured_network
from flint.core._shared_rootfs import configured_pool
from flint.core.types import SandboxState, _SandboxEntry
from .gateway import gateway_request
from .migration_transport import MigrationRelay


class LiveMigration:
    def __init__(self, agent):
        self.agent = agent
        self.daemon = agent.daemon
        self.relays: dict[str, MigrationRelay] = {}
        self.workers: dict[str, threading.Thread] = {}
        self.lock = threading.Lock()

    def _persist(self, entry):
        self.daemon._state_store.update_sandbox(
            entry.vm_id,
            backend_meta_json=json.dumps(entry.backend_metadata),
            availability=entry.availability,
        )

    @staticmethod
    def _version(socket_path):
        code, raw = ch.ch_get(socket_path, "/api/v1/vmm.ping")
        if code != 200:
            raise RuntimeError("VMM version unavailable")
        return json.loads(raw)["version"]

    def prepare(self, vm_id: str, body: dict) -> dict:
        if self.daemon.backend.kind != "linux-cloud-hypervisor":
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
        jail = ChJail(vm_id)
        mgr = self.daemon.manager
        with self.lock:
            if mgr.get_entry(vm_id) or os.path.exists(jail.base):
                raise ValueError("destination already has this VM or a stale jail")
            relay = MigrationRelay()
            process = None
            namespace = ""
            try:
                jail.prepare()
                with open(f"{jail.base}/shared-rootfs", "x") as marker:
                    marker.write(pool.pool_id)
                disk = pool.attach(
                    vm_id, jail.host_path(JAIL_ROOTFS), expected=body["root_disk"]
                )
                gateway_request("prepare", vm_id, internet=body["internet"])
                namespace, tap = net.setup_compute(body["internet"])
                process = jail.spawn(namespace)
                entry = _SandboxEntry(
                    vm_id=vm_id,
                    process=process,
                    pid=process.pid,
                    vm_dir=jail.root,
                    socket_path=jail.socket_path,
                    ns_name=namespace,
                    guest_ip=config.GUEST_IP,
                    agent_url=f"http://{config.GUEST_IP}:{config.AGENT_PORT}",
                    agent_healthy=False,
                    state=SandboxState.STARTING,
                    chroot_base=jail.base,
                    backend_kind=self.daemon.backend.kind,
                    template_id=body["template_id"],
                    tenant=body["tenant"],
                    # Even ephemeral receivers are fenced until the CP commits.
                    availability=body["availability"]
                    if body["availability"] in ("durable", "restartable")
                    else "restartable",
                    backend_metadata={
                        "api_socket": jail.socket_path,
                        "ns_name": namespace,
                        "tap_name": tap,
                        "veth_ip": "",
                        "resources": body["resources"],
                        "maintenance": {
                            "root_disk": disk,
                            "network": body["network"],
                            "live_phase": "receiving",
                            "original_availability": body["availability"],
                        },
                    },
                )
                self.daemon._state_store.insert_sandbox(
                    vm_id=vm_id,
                    pid=process.pid,
                    vm_dir=jail.root,
                    socket_path=jail.socket_path,
                    ns_name=namespace,
                    state=SandboxState.STARTING,
                    daemon_pid=os.getpid(),
                    template_id=entry.template_id,
                    tenant=entry.tenant,
                    availability=entry.availability,
                    backend_kind=entry.backend_kind,
                    chroot_base=jail.base,
                    backend_meta_json=entry.backend_metadata,
                )
                with mgr._lock:
                    mgr._sandboxes[vm_id] = entry
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
                        entry.backend_metadata["maintenance"]["live_phase"] = "cutover"
                        entry.backend_metadata["maintenance"]["live_executed"] = True
                        self._persist(entry)
                        gateway_request("activate", vm_id)
                        net.announce()
                        entry.backend_metadata["maintenance"]["live_phase"] = "received"
                        entry.state = SandboxState.RUNNING
                        entry.agent_healthy = True
                        self._persist(entry)
                        self.daemon._state_store.transition_state(
                            vm_id, SandboxState.RUNNING
                        )
                    except Exception as exc:
                        if entry.backend_metadata["maintenance"].get("live_executed"):
                            entry.backend_metadata["maintenance"]["live_phase"] = (
                                "cutover_failed"
                            )
                        entry.backend_metadata["maintenance"]["live_error"] = str(exc)
                        self._persist(entry)
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
                    from flint.core._netns import _delete_netns

                    _delete_netns(namespace)
                jail.cleanup()
                with mgr._lock:
                    mgr._sandboxes.pop(vm_id, None)
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
        maintenance = entry.backend_metadata.get("maintenance", {})
        if not maintenance.get("root_disk") or not maintenance.get("network"):
            raise ValueError("VM was not booted with migration storage and networking")
        relay = MigrationRelay()
        path = os.path.join(entry.vm_dir, "migration-send.sock")
        maintenance["live_phase"] = "sending"
        self._persist(entry)
        self.relays[vm_id] = relay
        t0 = time.monotonic()
        try:
            relay.sender(path, body["address"], body["port"], body["target_host"])
            ch.vm_send_migration(entry.socket_path, "unix:/migration-send.sock")
            entry.state = SandboxState.PAUSED
            maintenance["live_phase"] = "sent"
            self._persist(entry)
            self.daemon._state_store.transition_state(vm_id, SandboxState.PAUSED)
            return {"transferred": True, "total_ms": (time.monotonic() - t0) * 1000}
        except Exception:
            maintenance["live_phase"] = "send_failed"
            self._persist(entry)
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
        meta = entry.backend_metadata.get("maintenance", {})
        state = "Unknown"
        if meta.get("live_phase") in ("receiving", "cutover"):
            return {
                "phase": meta["live_phase"],
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
            "phase": meta.get("live_phase", "none"),
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
            meta = (
                entry.backend_metadata
                if entry
                else json.loads((row or {}).get("backend_meta_json") or "{}")
            )
            executed = bool(
                meta.get("maintenance", {}).get("live_executed")
            ) or meta.get("maintenance", {}).get("live_phase") in (
                "received",
                "cutover_failed",
            )
            self.daemon.kill_vm(vm_id)
            worker = self.workers.get(vm_id)
            if worker:
                worker.join(timeout=5)
                if worker.is_alive():
                    raise RuntimeError("migration receiver has not stopped")
            if self._alive(pid):
                raise RuntimeError("receiver process death is unconfirmed")
            if not row and os.path.exists(ChJail(vm_id).base):
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
        entry.state = SandboxState.RUNNING
        entry.backend_metadata.setdefault("maintenance", {})["live_phase"] = (
            "rolled_back"
        )
        self._persist(entry)
        self.daemon._state_store.transition_state(vm_id, SandboxState.RUNNING)
        self.agent.fence_started(vm_id)
        return {"vm": entry.to_dict()}

    def commit(self, vm_id: str) -> dict:
        entry = self.daemon.manager.get_entry(vm_id)
        if (
            not entry
            or entry.backend_metadata.get("maintenance", {}).get("live_phase")
            != "received"
        ):
            raise ValueError("receiver has not completed")
        entry.availability = entry.backend_metadata["maintenance"][
            "original_availability"
        ]
        self._persist(entry)
        self.agent._arm_fence()
        return {"vm": entry.to_dict()}

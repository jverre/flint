"""The daemon as a cluster host agent.

Runs next to the daemon's API (which the control plane drives over mTLS):

- heartbeats every 2s with capacity, VM states, cached snapshots and local
  templates; applies the evictions the control plane answers with;
- renews the fence in snapd only after a heartbeat succeeded, so durable and
  restartable VMs die by the time their lease lapses if this host loses the
  control plane (or this process hangs);
- forwards the daemon's events;
- publishes built templates to the snapshot store, exports pause snapshots,
  and restores VMs and templates from snapshots taken on other hosts.
"""

from __future__ import annotations

import collections
import contextlib
import json
import os
import platform
import shutil
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid

from flint.core import config
from flint.core.config import log
from flint.core.types import SandboxState
from flint.daemon import events as _events

from .snapd import Snapd, SnapdError
from .maintenance import PatchInventory

HEARTBEAT_INTERVAL = 2.0
LEASED = ("durable", "restartable")  # availability classes fenced on lease loss
VERSION = "0.1.0"


def host_class(backend) -> str:
    """Hosts of one class can restore each other's memory snapshots."""
    cpu = "unknown-cpu"
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    cpu = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    vmm = "unknown-vmm"
    binary = config.FIRECRACKER_BINARY if backend.kind == "linux-firecracker" else config.CH_BINARY
    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=5).stdout
        vmm = out.strip().splitlines()[0] if out.strip() else vmm
    except Exception:
        pass
    migration = ""
    if os.environ.get("FLINT_SHARED_ROOTFS_DIR"):
        from flint.core._shared_rootfs import configured_pool
        pool = configured_pool()
        migration = f"|pool={pool.pool_id}|gateway={os.environ.get('FLINT_MIGRATION_GATEWAY_IP','')}"
    group = os.environ.get("FLINT_MIGRATION_COMPATIBILITY_GROUP", "")
    if group and backend.kind == "linux-cloud-hypervisor":
        return f"{backend.kind}|{cpu}|validated:{group}{migration}"
    return f"{backend.kind}|{cpu}|{platform.release()}|{vmm}{migration}"


def capacity() -> dict:
    mem_total = mem_avail = 0
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                if k == "MemTotal":
                    mem_total = int(v.split()[0]) // 1024
                elif k == "MemAvailable":
                    mem_avail = int(v.split()[0]) // 1024
    except OSError:
        pass
    try:
        st = os.statvfs(config.JAILER_BASE_DIR)
        disk_free = st.f_bavail * st.f_frsize // (1 << 20)
    except OSError:
        disk_free = 0
    from flint.core import _netns

    max_vms = config.MAX_VMS or max(1, min(250, mem_total // 256))
    return {
        "max_vms": max_vms, "vcpus": os.cpu_count() or 1, "mem_total_mb": mem_total,
        "mem_available_mb": mem_avail, "disk_free_mb": disk_free,
        "netns_free": 253 - len(_netns._veth_ips),
        "vm_vcpus": config.CH_CPU_COUNT if os.environ.get("FLINT_BACKEND") == "cloud-hypervisor" else 1,
        "vm_memory_mb": max(128, config.CH_MEMORY_BYTES // (1 << 20)) if os.environ.get("FLINT_BACKEND") == "cloud-hypervisor" else 128,
        "reserve_vcpus": int(os.environ.get("FLINT_RESERVE_VCPUS", "0")),
        "reserve_memory_mb": int(os.environ.get("FLINT_RESERVE_MEMORY_MB", "256")),
        "reserve_disk_mb": int(os.environ.get("FLINT_RESERVE_DISK_MB", "1024")),
    }


class ClusterAgent:
    def __init__(self, daemon) -> None:
        self.daemon = daemon
        self.dir = config.CLUSTER_IDENTITY_DIR
        with open(os.path.join(self.dir, "host.json")) as f:
            self.identity = json.load(f)
        self.host_id = self.identity["host_id"]
        self.server = self.identity["server"].rstrip("/")
        self.snapd = Snapd(config.SNAPD_SOCKET)
        self.host_class = host_class(daemon.backend)
        self._inventory = PatchInventory()
        from .live_migration import LiveMigration
        self.migration = LiveMigration(self)
        self._handover_token = ""
        self.lease_ttl_ms = 15000
        self._events: collections.deque = collections.deque(maxlen=20000)
        self._stop = threading.Event()
        self._ctx: ssl.SSLContext | None = None
        self._ctx_mtime = 0.0
        self._lock = threading.Lock()
        # template id -> published snapshot id (or the one it was imported from)
        self._templates_path = os.path.join(self.dir, "templates.json")
        self._templates = self._load_json(self._templates_path)
        # vm id -> pause snapshot id exported for its current pause
        self._pauses_path = os.path.join(self.dir, "pauses.json")
        self._pauses = self._load_json(self._pauses_path)
        self._exporting: set[str] = set()
        self._api_pausing: set[str] = set()
        self.last_lease_ok = 0.0
        self._lease_path = os.path.join(self.dir, "lease.json")
        # When the current lease runs out (time.monotonic()); the fence in
        # snapd always expires with it. Starts from the persisted lease, so a
        # restarted agent can fence VMs it starts before its first heartbeat.
        lease = self._load_json(self._lease_path)
        expires = lease.get("renewed_at", 0.0) + lease.get("ttl_ms", 0) / 1000
        self._lease_deadline = time.monotonic() + (expires - time.time())
        # Serialises fence updates from heartbeats and from VM starts, so an
        # older list of VMs never overwrites a newer one.
        self._fence_lock = threading.Lock()

    # ── setup ────────────────────────────────────────────────────────────

    @staticmethod
    def _load_json(path: str) -> dict:
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_json(self, path: str, data: dict) -> None:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)

    def start(self) -> None:
        if os.path.exists(os.path.join(self.dir, "handover.json")):
            self._handover_token = self.snapd._call("POST", "/v1/handover/claim", timeout=5).get("token", "")
            lease = self._load_json(self._lease_path)
            expires = lease.get("renewed_at", 0.0) + lease.get("ttl_ms", 0) / 1000
            self._lease_deadline = time.monotonic() + (expires - time.time())
        self._fence_lapsed_leases()
        _events.hooks.append(self._events.append)
        threading.Thread(target=self._heartbeat_loop, daemon=True, name="cluster-heartbeat").start()
        threading.Thread(target=self._events_loop, daemon=True, name="cluster-events").start()
        log.info("cluster agent: host %s, server %s, class %s", self.host_id, self.server, self.host_class)

    def stop(self) -> None:
        self._stop.set()

    def server_ssl(self) -> dict:
        """uvicorn TLS settings: our host cert; only control-plane clients."""
        return {
            "ssl_keyfile": os.path.join(self.dir, "host.key"),
            "ssl_certfile": os.path.join(self.dir, "host.crt"),
            "ssl_ca_certs": os.path.join(self.dir, "cp-ca.pem"),
            "ssl_cert_reqs": ssl.CERT_REQUIRED,
        }

    def _client_ctx(self) -> ssl.SSLContext:
        """Client TLS towards the control plane, reloaded when snapd renews our cert."""
        crt = os.path.join(self.dir, "host.crt")
        mtime = os.path.getmtime(crt)
        if self._ctx is None or mtime != self._ctx_mtime:
            ctx = ssl.create_default_context(cafile=os.path.join(self.dir, "cp-ca.pem"))
            ctx.load_cert_chain(crt, os.path.join(self.dir, "host.key"))
            self._ctx, self._ctx_mtime = ctx, mtime
        return self._ctx

    def _post(self, path: str, body: dict, timeout: float = 10.0) -> dict:
        req = urllib.request.Request(
            self.server + path, data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=self._client_ctx()) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"control plane {path}: {e.code} {e.read()[:300]!r}") from None

    # ── heartbeat, lease, fence ────────────────────────────────────────

    def _fence_lapsed_leases(self) -> None:
        """At startup: leased VMs whose lease ran out while this agent was down
        may already run elsewhere (the control plane recovered them), so they
        must not keep running here. Covers the agent and snapd dying together."""
        lease = self._load_json(self._lease_path)
        renewed, ttl = lease.get("renewed_at", 0.0), lease.get("ttl_ms", self.lease_ttl_ms) / 1000
        if time.time() - renewed <= ttl:
            return
        mgr = self.daemon.manager
        with mgr._lock:
            stale = [e.vm_id for e in mgr._sandboxes.values() if e.availability in LEASED]
        for vm_id in stale:
            log.warning("cluster: lease of %s lapsed %.0fs ago while the agent was down; killing it", vm_id[:8], time.time() - renewed - ttl)
            try:
                self.daemon.kill_vm(vm_id)
            except Exception:
                log.exception("fencing %s failed", vm_id[:8])

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(HEARTBEAT_INTERVAL):
            try:
                self.heartbeat_once()
            except Exception as e:
                # No renewal: the fence deadline set after the last good
                # heartbeat stands, and snapd fires it on time.
                log.warning("cluster heartbeat failed: %s", e)
            try:
                self._export_unexported_pauses()
                self._publish_templates()
            except Exception:
                log.exception("cluster housekeeping failed")

    def heartbeat_once(self) -> None:
        sent = time.monotonic()
        try:
            cached = self.snapd.cache().get("complete", [])
        except SnapdError:
            cached = []
        resp = self._post("/v1/hosts/self/heartbeat", {
            "version": VERSION,
            "handover_token": self._handover_token,
            "backend": self.daemon.backend.kind,
            "host_class": self.host_class,
            "capacity": capacity(),
            "patch_state": self._patch_state(),
            "vms": self._vm_reports(),
            "cached_snapshots": cached,
            "templates": self._template_reports(),
        })
        self.lease_ttl_ms = int(resp.get("lease_ttl_ms", self.lease_ttl_ms))
        # The lease runs from when we *sent* the heartbeat, so our deadline is
        # never later than the one the control plane counts from receipt.
        self._lease_deadline = sent + self.lease_ttl_ms / 1000
        self._arm_fence()
        self.last_lease_ok = time.time()
        # Persisted so a restarted agent knows whether its leases lapsed meanwhile;
        # backdated to the send time like the fence deadline.
        self._save_json(self._lease_path, {"renewed_at": self.last_lease_ok - (time.monotonic() - sent), "ttl_ms": self.lease_ttl_ms})
        if self._handover_token:
            self.snapd._call("DELETE", "/v1/handover", {"token": self._handover_token}, timeout=5)
            self._handover_token = ""
        for vm_id in resp.get("evict") or []:
            log.info("cluster: evicting %s (owned elsewhere or deleted)", vm_id[:8])
            try:
                self.daemon.kill_vm(vm_id)
            except Exception:
                log.exception("evict %s failed", vm_id[:8])
            with self._lock:
                self._pauses.pop(vm_id, None)

    def _arm_fence(self) -> bool:
        """Point snapd's fence at the leased VMs running here now, expiring
        with the current lease. False if the lease has run out: snapd keeps
        its last deadline, which has passed or is about to."""
        mgr = self.daemon.manager
        with self._fence_lock:
            with mgr._lock:
                leased = [{"vm_id": e.vm_id, "pid": e.pid} for e in mgr._sandboxes.values()
                          if e.state in (SandboxState.RUNNING, SandboxState.STARTING) and e.availability in LEASED and e.pid > 0]
            remaining = int((self._lease_deadline - time.monotonic()) * 1000)
            if not leased:
                self.snapd.disarm()
            elif remaining > 0:
                self.snapd.fence(remaining, leased)
            else:
                return False
            return True

    def fence_started(self, vm_id: str) -> None:
        """Fence a leased VM that just started here (created, restored or
        resumed) before the API answers. Waiting for the next heartbeat would
        leave it unfenced for up to HEARTBEAT_INTERVAL: if this agent hung
        then, the control plane would recover it elsewhere while this copy
        kept running. Raises if it can't be fenced; the caller kills it."""
        entry = self.daemon.manager.get_entry(vm_id)
        if entry is None or entry.availability not in LEASED:
            return
        if not self._arm_fence():
            raise RuntimeError("this host's lease has lapsed")

    def _patch_state(self) -> dict:
        mgr = self.daemon.manager
        with mgr._lock:
            processes = {e.vm_id: e.pid for e in mgr._sandboxes.values()
                         if e.state == SandboxState.RUNNING and e.pid > 1}
        binary = config.FIRECRACKER_BINARY if self.daemon.backend.kind == "linux-firecracker" else config.CH_BINARY
        return self._inventory.report(binary, processes)

    def _vm_reports(self) -> list[dict]:
        mgr = self.daemon.manager
        out = {}
        with mgr._lock:
            for e in mgr._sandboxes.values():
                out[e.vm_id] = {"vm_id": e.vm_id, "state": str(e.state),
                                "resources": e.backend_metadata.get("resources", {}),
                                "maintenance": e.backend_metadata.get("maintenance", {})}
        for row in self.daemon._state_store.list_active():
            if row["vm_id"] in out:
                continue
            rep = {"vm_id": row["vm_id"], "state": row["state"]}
            if row["vm_id"] in self._api_pausing:
                rep["state"] = SandboxState.RUNNING.value
            elif row["state"] == SandboxState.PAUSED.value and row["vm_id"] in self._pauses:
                rep["pause_snapshot_id"] = self._pauses[row["vm_id"]]
            out[row["vm_id"]] = rep
        return list(out.values())

    # ── events ──────────────────────────────────────────────────────────

    def _events_loop(self) -> None:
        while not self._stop.wait(0.3):
            batch = []
            while self._events and len(batch) < 500:
                batch.append(self._events.popleft())
            if not batch:
                continue
            try:
                self._post("/v1/hosts/self/events", {"events": batch})
            except Exception as e:
                log.warning("cluster: forwarding %d events failed: %s", len(batch), e)
                self._events.extendleft(reversed(batch))
                self._stop.wait(2.0)

    # ── snapshots ───────────────────────────────────────────────────────

    def _staging(self, prefix: str) -> str:
        # On the jail filesystem, so restored files are renamed into place.
        d = os.path.join(config.JAILER_BASE_DIR, ".restore", f"{prefix}-{uuid.uuid4().hex[:8]}")
        os.makedirs(d, mode=0o700)
        return d

    def export_pause(self, vm_id: str, snapshot_id: str, upload: str = "sync") -> dict:
        """Snapshot a paused VM's files into the store."""
        row = self.daemon._state_store.get_sandbox(vm_id)
        if not row or row["state"] != SandboxState.PAUSED.value:
            raise RuntimeError(f"{vm_id} is not paused")
        root, files = self.daemon.backend.pause_files(row)
        res = self.snapd.create(snapshot_id, "pause", root, files, upload=upload,
                                meta={"vm_id": vm_id, "template_id": row.get("template_id", "default")})
        with self._lock:
            self._pauses[vm_id] = snapshot_id
            self._save_json(self._pauses_path, self._pauses)
        return res["snapshot"]

    def _export_unexported_pauses(self) -> None:
        """A VM paused by its own timeout policy still gets a snapshot, so it can resume elsewhere."""
        for row in self.daemon._state_store.list_in_state(SandboxState.PAUSED):
            vm_id = row["vm_id"]
            if vm_id in self._pauses or vm_id in self._exporting or vm_id in self._api_pausing:
                continue
            self._exporting.add(vm_id)
            try:
                self.export_pause(vm_id, f"pause-{uuid.uuid4()}")
            except Exception as e:
                log.warning("cluster: exporting pause of %s failed: %s", vm_id[:8], e)
            finally:
                self._exporting.discard(vm_id)

    @contextlib.contextmanager
    def api_pause(self, vm_id: str):
        """A pause the control plane asked for, export included. Until it
        returns, the VM keeps reporting Running: the control plane learns the
        outcome from the response, and a failed export resumes the VM."""
        with self._lock:
            self._api_pausing.add(vm_id)
        try:
            yield
        finally:
            with self._lock:
                self._api_pausing.discard(vm_id)

    def forget_pause(self, vm_id: str) -> None:
        with self._lock:
            if self._pauses.pop(vm_id, None) is not None:
                self._save_json(self._pauses_path, self._pauses)

    def checkpoint(self, vm_id: str, snapshot_id: str, kind: str, parent: str = "", upload: str = "sync") -> dict:
        """Snapshot a running VM; it's paused only while its state is read."""
        t0 = time.monotonic()
        timings: dict[str, float] = {}

        def take(root: str, files: dict[str, str]) -> dict:
            t = time.monotonic()
            res = self.snapd.create(snapshot_id, kind, root, files, upload="async", parent=parent,
                                    meta={"vm_id": vm_id})
            timings["snapshot_ms"] = (time.monotonic() - t) * 1000
            return res

        res = self.daemon.manager.checkpoint(vm_id, take)
        timings["paused_ms"] = (time.monotonic() - t0) * 1000
        snap = res["snapshot"]
        snap["uploaded"] = False
        if upload == "sync":
            t = time.monotonic()
            self.snapd.wait_uploaded(snapshot_id)
            timings["upload_ms"] = (time.monotonic() - t) * 1000
            snap["uploaded"] = True
        return {"snapshot": snap, "timings": timings}

    def restore_vm(self, vm_id: str, ref: dict, **kwargs) -> str:
        """Start vm_id from a snapshot taken anywhere in the cluster."""
        staging = self._staging(vm_id[:8])
        try:
            self.snapd.restore(ref["id"], staging, ref.get("peers") or [])
            return self.daemon.manager.restore(vm_id, staging, **kwargs)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    # ── templates ───────────────────────────────────────────────────────

    def _template_reports(self) -> list[dict]:
        from flint.core._template_registry import list_templates

        out = []
        kind = self.daemon.backend.kind
        for t in list_templates():
            art = (t.get("artifacts") or {}).get(kind)
            if not art or t["template_id"] == config.DEFAULT_TEMPLATE_ID:
                continue
            status = art.get("status")
            status = "error" if status == "failed" else status
            rep = {"id": t["template_id"], "status": status}
            pub = self._templates.get(t["template_id"])
            if pub and status == "ready":
                rep["snapshot_id"] = pub
            out.append(rep)
        return out

    def _publish_templates(self) -> None:
        from flint.core._template_registry import get_template_dir, list_templates

        kind = self.daemon.backend.kind
        for t in list_templates():
            tid = t["template_id"]
            art = (t.get("artifacts") or {}).get(kind)
            if tid == config.DEFAULT_TEMPLATE_ID or not art or art.get("status") != "ready" or tid in self._templates:
                continue
            tdir = get_template_dir(tid, backend_kind=kind)
            files = {n: n for n in self.daemon.backend.template_files(tdir)}
            snap_id = f"tpl-{tid}-{uuid.uuid4().hex[:12]}"
            log.info("cluster: publishing template %s as %s", tid, snap_id)
            self.snapd.create(snap_id, "template", tdir, files, upload="sync", meta={"template_id": tid})
            with self._lock:
                self._templates[tid] = snap_id
                self._save_json(self._templates_path, self._templates)

    def ensure_template(self, template_id: str, ref: dict) -> None:
        """Install a template built on another host from its snapshot."""
        from flint.core._template_registry import register_template_artifact, template_snapshot_exists

        kind = self.daemon.backend.kind
        if template_snapshot_exists(template_id, backend_kind=kind):
            return
        with self._lock:  # one import per template at a time
            if template_snapshot_exists(template_id, backend_kind=kind):
                return
            dest = os.path.join(config.TEMPLATES_DIR, template_id)
            staging = os.path.join(config.TEMPLATES_DIR, f".import-{template_id}-{uuid.uuid4().hex[:8]}")
            try:
                self.snapd.restore(ref["id"], staging, ref.get("peers") or [])
                for root, dirs, files in os.walk(staging):
                    os.chmod(root, 0o755)
                    for n in files:
                        os.chmod(os.path.join(root, n), 0o644)
                shutil.rmtree(dest, ignore_errors=True)
                os.rename(staging, dest)
            finally:
                shutil.rmtree(staging, ignore_errors=True)
            register_template_artifact(template_id, template_id, kind, dest, status="ready")
            self._templates[template_id] = ref["id"]
            self._save_json(self._templates_path, self._templates)
            log.info("cluster: installed template %s from snapshot %s", template_id, ref["id"])

    def forget_template(self, template_id: str) -> None:
        with self._lock:
            if self._templates.pop(template_id, None) is not None:
                self._save_json(self._templates_path, self._templates)

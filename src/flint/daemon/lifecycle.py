"""The daemon process: start every part in order, recover VMs, serve the API, shut down.

How the API is served depends on the host:

* a cluster host agent serves it over mTLS, to the control plane only;
* a backend that confines its VMMs as host processes (Linux) runs as root
  and serves the public API from a separate unprivileged process
  (``supervisor.py``), which calls back into this one;
* otherwise (macOS) the API runs in this process.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time

import uvicorn

from flint.core.backends import resolve_backend
from flint.core.config import (
    ADMIN_TOKEN_PATH, DAEMON_DB_PATH, DAEMON_DIR, DAEMON_HOST, DAEMON_PID_PATH, DAEMON_PORT, DAEMON_SOCKET,
    DAEMON_STATE_PATH, DEFAULT_TEMPLATE_ID, ERROR_CLEANUP_DELAY, HEALTH_CHECK_INTERVAL, TEMPLATES_DIR,
)
from flint.core import _nfs_server

from . import events
from .listeners import listen_tcp, listen_unix
from .services import LocalServices

log = logging.getLogger("flint.daemon")


def configure_logging() -> None:
    """Send the daemon's log (INFO and up, every ``flint.*`` logger) to stderr."""
    root = logging.getLogger("flint")
    if any(getattr(h, "_flint_stderr", False) for h in root.handlers):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S"))
    handler.setLevel(logging.INFO)
    handler._flint_stderr = True
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)


class FlintDaemon:
    def __init__(self) -> None:
        self.backend = resolve_backend()
        self.manager = None  # SandboxManager
        self.state_store = None  # StateStore
        self.golden_ready: bool = False
        self.started_at: float = 0.0
        self.storage = None
        self.volumes = None  # core._volumes.Volumes
        self.metrics = None  # core._metrics.MetricsSampler
        self.cluster = None  # ClusterAgent when running as a cluster host agent
        self.tokens = None  # auth.TokenStore, unless the unprivileged API owns them
        self.eventlog = None  # eventlog.EventLog
        self.quotas = None  # core._quotas.Quotas
        self.services = LocalServices(self)
        # Who serves the public API: this process, or the unprivileged one (supervisor.py).
        self.api_pid = os.getpid()
        self.api_uid = os.geteuid()
        self._health_monitor = None
        self._lifecycle_manager = None
        self.api_process = None  # the unprivileged API's process (supervisor.py)
        self._daemon_lock: int | None = None
        self._state_lock = threading.Lock()

    def run(self) -> None:
        configure_logging()
        self.started_at = time.time()
        self._init_dirs()
        self._write_pid()
        self._prepare_backend()
        self._init_nfs_server()
        self._init_storage()
        self._start_pool()
        self._init_state_store()
        self._init_manager()
        self._recover_sandboxes()
        self.volumes.replay()
        self._start_health_monitor()
        self._start_lifecycle()
        self._start_cluster_agent()
        self._install_signal_handlers()
        self._serve()

    def write_state(self) -> None:
        """Atomically write the daemon's state (its VMs) to ``state.json``.

        Requests run in a thread pool, so concurrent creates/kills call this at
        the same time. The lock keeps them from sharing the temp file, and
        because the snapshot is taken inside it, the last write always holds
        the latest state.
        """
        with self._state_lock:
            state = {
                "pid": os.getpid(),
                "started_at": self.started_at,
                "golden_snapshot_ready": self.golden_ready,
                "vms": {d["vm_id"]: d for d in self.manager.list_dicts()} if self.manager else {},
                "api_pid": self.api_pid,
                "api_uid": self.api_uid,
            }
            tmp_path = DAEMON_STATE_PATH + ".tmp"
            with open(tmp_path, "w") as f:
                json.dump(state, f)
            os.replace(tmp_path, DAEMON_STATE_PATH)

    # ── Start-up ─────────────────────────────────────────────────────────

    def _init_dirs(self) -> None:
        os.makedirs(DAEMON_DIR, exist_ok=True)
        if not self.backend.supports_confined_api:
            return
        import fcntl
        self._daemon_lock = os.open(os.path.join(DAEMON_DIR, "daemon.lock"),
                                    os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(self._daemon_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Another Flint runtime owns this state directory")

        def close_in_helper():
            # A namespace helper must not keep the daemon lease alive
            # after its supervisor exits or crashes.
            if self._daemon_lock is not None:
                os.close(self._daemon_lock)
                self._daemon_lock = None
        os.register_at_fork(after_in_child=close_in_helper)

    def _write_pid(self) -> None:
        with open(DAEMON_PID_PATH, "w") as f:
            f.write(str(os.getpid()))

    def _prepare_backend(self) -> None:
        from flint.core._template_registry import get_template
        os.makedirs(TEMPLATES_DIR, exist_ok=True)
        self.backend.ensure_runtime_ready()
        self.backend.ensure_default_template()
        default_template = get_template(DEFAULT_TEMPLATE_ID) or {}
        artifact = (default_template.get("artifacts") or {}).get(self.backend.kind) or {}
        self.golden_ready = artifact.get("status") == "ready"
        log.info("Backend ready: %s", self.backend.kind)

    def _init_nfs_server(self) -> None:
        if not self.backend.supports_volume_mounts:
            return  # No bridge network to serve volumes on.
        try:
            _nfs_server.ensure_running()
            log.info("NFS server ready.")
        except _nfs_server.NfsServerError as e:
            log.warning("NFS server unavailable, volumes can't be mounted: %s", e)

    def _init_storage(self) -> None:
        from flint.core.storage import get_storage_backend
        self.storage = get_storage_backend()
        self.storage.start()
        log.info("Storage backend: %s", self.storage.kind)

    def _start_pool(self) -> None:
        self.backend.start_pool()
        log.info("Backend pool started.")

    def _init_state_store(self) -> None:
        from flint.core import config
        from flint.core._quotas import Quotas
        from flint.core._state_store import StateStore
        from .auth import TokenStore
        from .eventlog import EventLog
        self.state_store = StateStore(DAEMON_DB_PATH, publish=events.publish)
        # Once the unprivileged API has its own database, it alone holds the tokens.
        separate_auth = self.backend.supports_confined_api and not config.CLUSTER_MODE
        migrated = os.path.isfile(os.path.join(DAEMON_DIR, "api", "flint.db"))
        self.tokens = None if separate_auth and migrated else TokenStore(DAEMON_DB_PATH)
        self.eventlog = EventLog(DAEMON_DB_PATH)
        events.sink = self.eventlog.record
        self.quotas = Quotas(DAEMON_DB_PATH)
        if not config.CLUSTER_MODE and self.tokens:  # a host agent's only caller is the control plane, over mTLS
            self.tokens.ensure_admin_token_file()
        log.info("State store initialized.")

    def _init_manager(self) -> None:
        from flint.core._metrics import MetricsSampler
        from flint.core._volumes import Volumes
        from flint.core.manager import SandboxManager
        self.manager = SandboxManager(backend=self.backend, state_store=self.state_store, publish=events.publish)
        self.volumes = Volumes(DAEMON_DB_PATH, self.manager, self.state_store)
        self.manager.kill_hooks.extend((self._release_mounts, self._release_storage))
        self.metrics = MetricsSampler(lambda: self.manager.entries() if self.manager else [], self.backend.usage)
        events.vm_tenant = lambda vm_id: (self.manager.get_dict(vm_id) or {}).get("tenant", "")
        self.write_state()

    def _release_mounts(self, vm_id: str) -> None:
        for m in self.volumes.release_vm(vm_id):
            events.publish("volume.detached", vm_id=vm_id, volume_id=m.volume_id, mount_path=m.path)

    def _release_storage(self, vm_id: str) -> None:
        if self.storage:
            row = self.state_store.get_sandbox(vm_id)
            self.storage.teardown_sandbox(vm_id, row.backend_metadata.get("veth_ip", "") if row else "")

    def _recover_sandboxes(self) -> None:
        from flint.core._recovery import RecoveryEngine
        report = RecoveryEngine(self.state_store, self.manager).recover()
        log.info("Recovery: %s", report)

    def _start_health_monitor(self) -> None:
        from flint.core._health import HealthMonitor
        self._health_monitor = HealthMonitor(state_store=self.state_store, manager=self.manager,
                                             interval=HEALTH_CHECK_INTERVAL)
        self._health_monitor.start()
        log.info("Health monitor started.")

    def _start_lifecycle(self) -> None:
        from flint.core._lifecycle import LifecycleManager
        self._lifecycle_manager = LifecycleManager(state_store=self.state_store, manager=self.manager,
                                                   interval=1.0, error_cleanup_delay=ERROR_CLEANUP_DELAY)
        self._lifecycle_manager.start()
        log.info("Lifecycle manager started.")

    def _start_cluster_agent(self) -> None:
        from flint.core import config
        if not config.CLUSTER_MODE:
            return
        from flint.cluster.agent import ClusterAgent
        self.cluster = ClusterAgent(self)
        self.cluster.start()
        log.info("Cluster host agent: host %s, control plane %s", self.cluster.host_id, self.cluster.server)

    def _install_signal_handlers(self) -> None:
        owner_pid = os.getpid()

        def _shutdown(signum, frame):
            # pyroute2 can fork namespace helpers from worker threads. Their
            # termination must not run the supervisor's shutdown routine or
            # signal the API process whose handle they inherited.
            if os.getpid() != owner_pid:
                signal.signal(signum, signal.SIG_DFL)
                os.kill(os.getpid(), signum)
                return
            log.info("Shutting down (VMs will keep running)...")
            if self.api_process is not None and self.api_process.poll() is None:
                self.api_process.terminate()
            if self._health_monitor:
                self._health_monitor.stop()
            if self._lifecycle_manager:
                self._lifecycle_manager.stop()
            if self.cluster:
                self.cluster.stop()
            # Detach from VMs without killing them — they'll be recovered on restart
            if self.manager:
                self.manager.detach_all()
            if self.storage:
                self.storage.stop()
            self.backend.stop_pool()
            if self.state_store:
                self.state_store.close()
            for path in (DAEMON_STATE_PATH, DAEMON_PID_PATH, DAEMON_SOCKET):
                try:
                    os.unlink(path)
                except OSError:
                    pass
            raise SystemExit(0)

        signal.signal(signal.SIGINT, _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)

    # ── Serving the API ──────────────────────────────────────────────────

    def _serve(self) -> None:
        if self.cluster:
            self._serve_cluster()
        elif self.backend.supports_confined_api:
            from .supervisor import serve
            serve(self)
        else:
            self._serve_in_process()

    def _serve_in_process(self) -> None:
        from . import server
        server.app.state.services = self.services
        server.enable_sso()
        sockets = [listen_unix(DAEMON_SOCKET), listen_tcp(DAEMON_HOST, DAEMON_PORT)]
        log.info("Daemon ready on %s and http://%s:%s (API token required; admin token in %s) · web UI: `flint ui`",
                 DAEMON_SOCKET, DAEMON_HOST, DAEMON_PORT, ADMIN_TOKEN_PATH)
        # The timeout is a backstop: a stuck handler mustn't keep the daemon alive.
        api = uvicorn.Server(uvicorn.Config(server.app, log_level="warning", timeout_graceful_shutdown=5))
        if self.backend.needs_main_thread:
            # e.g. macOS's Virtualization.framework drives VMs from the main thread's RunLoop.
            threading.Thread(target=lambda: api.run(sockets=sockets), daemon=True, name="api").start()
            self.backend.run_main_loop()  # blocks forever
            return
        api.run(sockets=sockets)

    def _serve_cluster(self) -> None:
        """Serve over mTLS: only the control plane's certificates get in.
        The host certificate is reloaded when snapd renews it."""
        from . import server
        server.app.state.services = self.services
        cfg = uvicorn.Config(server.app, host=DAEMON_HOST, port=DAEMON_PORT, log_level="warning",
                             timeout_graceful_shutdown=5, **self.cluster.server_ssl())
        cfg.load()
        ctx, files = cfg.ssl, self.cluster.server_ssl()
        mtime = [os.path.getmtime(files["ssl_certfile"])]

        def _reload_cert():
            while True:
                time.sleep(30)
                try:
                    m = os.path.getmtime(files["ssl_certfile"])
                    if m != mtime[0]:
                        ctx.load_cert_chain(files["ssl_certfile"], files["ssl_keyfile"])
                        mtime[0] = m
                        log.info("cluster: reloaded host certificate")
                except Exception:
                    log.exception("cluster: certificate reload failed")

        threading.Thread(target=_reload_cert, daemon=True, name="cert-reload").start()
        uvicorn.Server(cfg).run()


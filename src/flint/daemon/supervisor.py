"""Start the public API in a separate UID and filesystem on Linux."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading

from flint.core import config
from flint.core._confinement import parser_command
from flint.core._identities import identity
from .runtime import Runtime, RuntimeServer


def _prepare_api_state(daemon, directory: Path, uid: int, gid: int) -> None:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chown(directory, uid, gid)
    database = directory / "flint.db"
    if not database.exists():
        # Migrate only authentication, never VM records or sealed policies.
        from .auth import _SCHEMA
        temporary = directory / "flint.db.tmp"
        temporary.unlink(missing_ok=True)
        with sqlite3.connect(temporary) as target:
            target.executescript(_SCHEMA)
            rows = daemon.tokens._conn.execute(
                "SELECT id, kind, secret_hash, tenants, name, created_at, expires_at, identity FROM api_tokens"
            ).fetchall()
            target.executemany("INSERT INTO api_tokens VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        temporary.chmod(0o600)
        os.chown(temporary, uid, gid)
        temporary.replace(database)
    if database.lstat().st_uid != uid or database.is_symlink():
        raise RuntimeError("Untrusted API state database")
    admin = directory / "admin.token"
    if not admin.exists() and os.path.isfile(config.ADMIN_TOKEN_PATH):
        fd = os.open(admin, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream, open(config.ADMIN_TOKEN_PATH) as source:
            stream.write(source.read(4096))
            os.fchown(stream.fileno(), uid, gid)
    # Materialize only the SSO secrets this service needs. The storage account,
    # host secret key, SSH credentials and daemon environment are not inherited.
    from .sso import CONFIG_PATH
    target = directory / "sso.json"
    if os.path.isfile(CONFIG_PATH):
        with open(CONFIG_PATH) as stream:
            settings = json.load(stream)
        for provider in settings.get("providers", []):
            variable = provider.pop("client_secret_env", "")
            if variable and not provider.get("client_secret"):
                provider["client_secret"] = os.environ.get(variable, "")
        temp = directory / "sso.json.tmp"
        # The API owns this directory after first boot. Do not follow planted
        # symlinks when refreshing configuration as root.
        temp.unlink(missing_ok=True)
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(settings, stream)
            os.fchown(stream.fileno(), uid, gid)
        temp.replace(target)
    else:
        target.unlink(missing_ok=True)


def serve(daemon) -> None:
    if os.geteuid() != 0:
        raise RuntimeError("The Linux runtime needs root; its public API runs under a separate unprivileged identity")
    from .server import _listen_unix, _listen_tcp
    uid, gid = identity("service:api:" + config._DATA_KEY)
    api_directory = Path(config.STATE_DIR) / "api"
    _prepare_api_state(daemon, api_directory, uid, gid)
    sockets = [_listen_unix(config.DAEMON_SOCKET), _listen_tcp(config.DAEMON_HOST, config.DAEMON_PORT)]
    path = os.path.join(config.STATE_DIR, "runtime.sock")
    # The public listener already excludes another active daemon.
    if os.path.lexists(path):
        os.unlink(path)
    runtime = Runtime(daemon, uid, sockets)
    server = RuntimeServer(path, runtime)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="runtime-rpc")
    thread.start()
    stop_metrics = threading.Event()
    def metrics():
        while not stop_metrics.wait(1):
            try:
                daemon.metrics._sample_all()
            except Exception:
                config.log.exception("Runtime metrics sampling failed")
    threading.Thread(target=metrics, daemon=True, name="runtime-metrics").start()
    readonly = {path: "/runtime.sock", str(Path(__file__).resolve().parents[2]): "/app"}
    ui = os.environ.get("FLINT_UI_DIR")
    if ui:
        readonly[os.path.realpath(ui)] = "/ui"
    command = ["/usr/bin/env", "PYTHONPATH=/app", "FLINT_STATE_DIR=/api", "FLINT_DATA_DIR=/data",
               f"FLINT_PORT={config.DAEMON_PORT}", f"FLINT_BACKEND={daemon.backend.name}"]
    if ui:
        command.append("FLINT_UI_DIR=/ui")
    command += [sys.executable, "-m", "flint.daemon.frontend", "/runtime.sock"]
    process = None
    try:
        process = subprocess.Popen(parser_command(uid, gid, readonly=readonly,
            writable={str(api_directory): "/api"}, command=command),
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}, start_new_session=True)
        daemon._api_process = process
        config.log.info("Public API supervisor pid=%d; parser uid=%d; privileged runtime pid=%d", process.pid, uid, os.getpid())
        code = process.wait()
        if code:
            raise RuntimeError(f"Unprivileged API exited with status {code}")
    finally:
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        stop_metrics.set()
        server.shutdown()
        server.server_close()
        for sock in sockets:
            sock.close()
        for filename in (path, config.DAEMON_SOCKET):
            try:
                os.unlink(filename)
            except FileNotFoundError:
                pass

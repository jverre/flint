"""Credential injection proxy for Flint sandboxes.

Runs a mitmproxy-based transparent proxy inside a VM's network namespace that
intercepts outbound HTTP/HTTPS requests and injects headers based on
domain-matching rules.

The proxy is launched as a `mitmdump` subprocess so it runs in its own process
and can be managed independently of the daemon's event loop.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
import select

from .config import log, HOST_IP, PROXY_PORT, PROXY_CA_DIR, STATE_DIR, _DATA_KEY
from ._netns import _popen_in_ns

# The proxy's rules hold credentials in plaintext, so they stay in memory
# (/run is a tmpfs) and readable by root only.
_RULES_DIR = f"/run/flint/proxy-{_DATA_KEY}" if os.path.isdir("/run") else os.path.join(STATE_DIR, "proxy-rules")
_CGROUP_ROOT = f"/sys/fs/cgroup/flint-proxies-{_DATA_KEY}"


def stop_namespace_proxy(ns_name: str) -> None:
    """Release a namespace's proxy even when its Python handle was lost."""
    if not ns_name:
        return
    from ._limits import stop_processes
    from ._wipe import wipe_tree
    group = Path(_CGROUP_ROOT) / ns_name
    stop_processes(str(group))
    if group.exists():
        group.rmdir()
    for path in Path(_RULES_DIR).glob(ns_name + "-*.json*"):
        wipe_tree(str(path))


def cleanup_abandoned_proxies() -> None:
    """Stop this daemon's old proxies before recovering their VM policies.

    The rules-file identity check also handles upgrades from proxies which
    predate cgroup ownership, without signalling a reused PID or another
    daemon/container's process.
    """
    rules = Path(_RULES_DIR)
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        fd = None
        try:
            fd = os.pidfd_open(int(proc.name))
            env = (proc / "environ").read_bytes().split(b"\0")
            value = next((e.split(b"=", 1)[1] for e in env if e.startswith(b"FLINT_PROXY_RULES=")), None)
            if value is None:
                continue
            path = Path(os.fsdecode(value))
            if path.parent != rules:
                continue
            if not os.path.samestat(path.stat(), (proc / "root" / str(path).lstrip("/")).stat()):
                continue
            signal.pidfd_send_signal(fd, signal.SIGKILL)
            poll = select.poll()
            poll.register(fd, select.POLLIN)
            if not poll.poll(10000):
                raise RuntimeError("Credential proxy has not stopped; retaining its rules")
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        finally:
            if fd is not None:
                os.close(fd)
    root = Path(_CGROUP_ROOT)
    if root.exists():
        for group in root.iterdir():
            if group.is_dir():
                stop_namespace_proxy(group.name)
    from ._wipe import wipe_tree
    wipe_tree(_RULES_DIR)


def _write_rules(path: str, rules: dict) -> None:
    """Write rules atomically, never readable by anyone but the daemon's user."""
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(rules, f)
    os.replace(tmp, path)


def _mitmdump() -> str:
    """mitmdump from the daemon's own environment, which needn't be on PATH."""
    beside = os.path.join(os.path.dirname(sys.executable), "mitmdump")
    return beside if os.path.exists(beside) else (shutil.which("mitmdump") or "mitmdump")


def _ensure_confdir() -> str:
    """Ensure the mitmproxy confdir exists. Returns the directory path.

    mitmproxy auto-generates its CA certificate and key on first run
    inside this directory. No manual certificate generation needed.
    """
    os.makedirs(PROXY_CA_DIR, exist_ok=True)
    return PROXY_CA_DIR


class CredentialProxy:
    """Manages a credential injection proxy for a single sandbox."""

    # The CA's public certificate (mitmproxy-ca.pem next to it holds the key).
    ca_cert_path = os.path.join(PROXY_CA_DIR, "mitmproxy-ca-cert.pem")

    def __init__(self, ns_name: str) -> None:
        self.ns_name = ns_name
        self._process: subprocess.Popen | None = None
        self._rules_file: str | None = None

    def start(self, rules: dict) -> None:
        """Start the mitmdump proxy subprocess in the sandbox's network namespace."""
        confdir = _ensure_confdir()

        os.makedirs(_RULES_DIR, mode=0o700, exist_ok=True)
        os.chmod(_RULES_DIR, 0o700)
        fd, rules_path = tempfile.mkstemp(prefix=f"{self.ns_name}-", suffix=".json", dir=_RULES_DIR)
        os.close(fd)
        _write_rules(rules_path, rules)
        self._rules_file = rules_path

        # Launch mitmdump in transparent mode inside the netns
        addon_script = os.path.join(os.path.dirname(__file__), "_proxy_server.py")
        cmd = [
            _mitmdump(),
            "--mode", "transparent",
            # Only the guest-facing TAP address: the bridge side must never reach it.
            "--listen-host", HOST_IP,
            "--listen-port", str(PROXY_PORT),
            "--set", f"confdir={confdir}",
            "--set", "connection_strategy=lazy",
            "-s", addon_script,
            "-q",  # quiet — suppress console output
        ]

        env = os.environ.copy()
        env["FLINT_PROXY_RULES"] = rules_path

        self._process = _popen_in_ns(
            self.ns_name, cmd,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
        try:
            from ._limits import attach_process
            attach_process(str(Path(_CGROUP_ROOT) / self.ns_name), self._process.pid)
            self._wait_listening()
        except Exception:
            self.stop()
            raise
        log.info("[%s] Credential proxy started (mitmdump pid=%d)", self.ns_name, self._process.pid)

    def _wait_listening(self, timeout: float = 15.0) -> None:
        """Block until mitmdump listens, so the guest's first request isn't refused."""
        # The process's own /proc net view is its namespace's; 0A is LISTEN.
        port = f":{PROXY_PORT:04X} "
        deadline = time.monotonic() + timeout
        while True:
            if self._process.poll() is not None:
                raise RuntimeError(f"Credential proxy exited with code {self._process.returncode}")
            try:
                with open(f"/proc/{self._process.pid}/net/tcp") as f:
                    if any(port in line and line.split()[3] == "0A" for line in f):
                        return
            except OSError:
                pass
            if time.monotonic() > deadline:
                raise RuntimeError(f"Credential proxy not listening after {timeout:.0f}s")
            time.sleep(0.05)

    def stop(self) -> None:
        """Stop the proxy subprocess."""
        if self._process:
            try:
                self._process.send_signal(signal.SIGTERM)
                self._process.wait(timeout=3)
            except Exception:
                try:
                    self._process.kill()
                    self._process.wait(timeout=2)
                except Exception:
                    pass
            log.info("[%s] Credential proxy stopped", self.ns_name)
            self._process = None
        if self._rules_file:
            self._rules_file = None
        stop_namespace_proxy(self.ns_name)

    def update_rules(self, rules: dict) -> None:
        """Update the proxy rules by rewriting the rules file.

        The addon watches the file mtime for changes on each request.
        """
        if self._rules_file:
            _write_rules(self._rules_file, rules)
            log.info("[%s] Credential proxy rules updated", self.ns_name)

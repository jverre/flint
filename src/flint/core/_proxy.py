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

from .config import log, HOST_IP, PROXY_PORT, STATE_DIR, _DATA_KEY
from ._netns import _popen_in_ns
from ._confinement import parser_command, supervised_spawn
from ._identities import identity, release

# The proxy's rules hold credentials in plaintext, so they stay in memory
# (/run is a tmpfs) and readable by root only.
_RULES_DIR = f"/run/flint/proxy-{_DATA_KEY}" if os.path.isdir("/run") else os.path.join(STATE_DIR, "proxy-rules")


def cleanup_abandoned() -> None:
    """The daemon lock is held and old proxies died with their supervisor."""
    from pathlib import Path
    from ._wipe import wipe_tree
    for directory in Path(_RULES_DIR).glob("*"):
        wipe_tree(str(directory))


def _write_rules(path: str, rules: dict, uid: int, gid: int) -> None:
    """Write rules atomically, never readable by anyone but the daemon's user."""
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        os.fchown(f.fileno(), uid, gid)
        json.dump(rules, f)
    os.replace(tmp, path)


def _mitmdump() -> str:
    """mitmdump from the daemon's own environment, which needn't be on PATH."""
    beside = os.path.join(os.path.dirname(sys.executable), "mitmdump")
    return beside if os.path.exists(beside) else (shutil.which("mitmdump") or "mitmdump")


class CredentialProxy:
    """Manages a credential injection proxy for a single sandbox."""

    def __init__(self, ns_name: str) -> None:
        self.ns_name = ns_name
        self._process: subprocess.Popen | None = None
        self._rules_file: str | None = None
        self._directory: str | None = None
        self.ca_cert_path = ""
        self.uid, self.gid = identity("proxy:" + ns_name)

    def start(self, rules: dict) -> None:
        """Start the mitmdump proxy subprocess in the sandbox's network namespace."""
        os.makedirs(_RULES_DIR, mode=0o700, exist_ok=True)
        os.chmod(_RULES_DIR, 0o700)
        directory = tempfile.mkdtemp(prefix=f"{self.ns_name}-", dir=_RULES_DIR)
        self._directory = directory
        os.chmod(directory, 0o555)
        confdir = os.path.join(directory, "conf")
        os.mkdir(confdir, 0o700)
        os.chown(confdir, self.uid, self.gid)
        self.ca_cert_path = os.path.join(confdir, "mitmproxy-ca-cert.pem")
        rules_path = os.path.join(directory, "rules.json")
        _write_rules(rules_path, rules, self.uid, self.gid)
        self._rules_file = rules_path

        # Launch mitmdump in transparent mode inside the netns
        addon_script = os.path.join(os.path.dirname(__file__), "_proxy_server.py")
        shutil.copyfile(addon_script, os.path.join(directory, "addon.py"))
        os.chmod(os.path.join(directory, "addon.py"), 0o444)
        cmd = [
            _mitmdump(),
            "--mode", "transparent",
            # Only the guest-facing TAP address: the bridge side must never reach it.
            "--listen-host", HOST_IP,
            "--listen-port", str(PROXY_PORT),
            "--set", "confdir=/proxy/conf",
            "--set", "connection_strategy=lazy",
            "-s", "/proxy/addon.py",
            "-q",  # quiet — suppress console output
        ]

        # The parent directory is root-only; the jail sees just this proxy's
        # rules and CA. No daemon environment or shared CA private key enters it.
        cmd = parser_command(self.uid, self.gid, readonly={directory: "/proxy"},
                             writable={confdir: "/proxy/conf"},
                             command=["/usr/bin/env", "FLINT_PROXY_RULES=/proxy/rules.json", *cmd])
        try:
            self._process = supervised_spawn(lambda: _popen_in_ns(
                self.ns_name, cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
            ))
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
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=2)
            log.info("[%s] Credential proxy stopped", self.ns_name)
            self._process = None
        if self._directory:
            from ._wipe import wipe_tree
            wipe_tree(self._directory)
            self._directory = None
            self._rules_file = None
        release("proxy:" + self.ns_name)

    def update_rules(self, rules: dict) -> None:
        """Update the proxy rules by rewriting the rules file.

        The addon watches the file mtime for changes on each request.
        """
        if self._rules_file:
            _write_rules(self._rules_file, rules, self.uid, self.gid)
            log.info("[%s] Credential proxy rules updated", self.ns_name)

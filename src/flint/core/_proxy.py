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
import signal
import subprocess
import tempfile

from .config import log, PROXY_PORT, PROXY_CA_DIR
from ._netns import _popen_in_ns


def _ensure_confdir() -> str:
    """Ensure the mitmproxy confdir exists. Returns the directory path.

    mitmproxy auto-generates its CA certificate and key on first run
    inside this directory. No manual certificate generation needed.
    """
    os.makedirs(PROXY_CA_DIR, exist_ok=True)
    return PROXY_CA_DIR


class CredentialProxy:
    """Manages a credential injection proxy for a single sandbox."""

    def __init__(self, ns_name: str) -> None:
        self.ns_name = ns_name
        self._process: subprocess.Popen | None = None
        self._rules_file: str | None = None

    def start(self, rules: dict) -> None:
        """Start the mitmdump proxy subprocess in the sandbox's network namespace."""
        confdir = _ensure_confdir()

        # Write rules to a temp file
        fd, rules_path = tempfile.mkstemp(prefix="flint-proxy-rules-", suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(rules, f)
        self._rules_file = rules_path

        # Launch mitmdump in transparent mode inside the netns
        addon_script = os.path.join(os.path.dirname(__file__), "_proxy_server.py")
        cmd = [
            "mitmdump",
            "--mode", "transparent",
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
        log.info("[%s] Credential proxy started (mitmdump pid=%d)", self.ns_name, self._process.pid)

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
            try:
                os.unlink(self._rules_file)
            except OSError:
                pass
            self._rules_file = None

    def update_rules(self, rules: dict) -> None:
        """Update the proxy rules by rewriting the rules file.

        The addon watches the file mtime for changes on each request.
        """
        if self._rules_file:
            tmp = self._rules_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(rules, f)
            os.rename(tmp, self._rules_file)
            log.info("[%s] Credential proxy rules updated", self.ns_name)

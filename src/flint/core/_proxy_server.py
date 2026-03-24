"""Credential injection mitmproxy addon.

Runs as a mitmproxy addon inside a VM's network namespace. Intercepts HTTP
and HTTPS traffic redirected via iptables and injects headers based on
domain-matching rules.

Usage (via mitmdump):
    mitmdump --mode transparent --set confdir=/path/to/ca \
        -s _proxy_server.py -q

The addon reads rules from the file path in the FLINT_PROXY_RULES environment
variable. Rules are a JSON dict mapping domain patterns to header dicts:
    {"api.openai.com": {"Authorization": "Bearer sk-..."}, "*.github.com": {...}}
"""

from __future__ import annotations

import fnmatch
import json
import os

from mitmproxy import http


def _load_rules(path: str) -> dict:
    """Load domain->headers rules from a JSON file."""
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def _match_domain(host: str, rules: dict) -> dict[str, str] | None:
    """Match a hostname against rules. Returns headers dict or None.

    Tries exact match first, then wildcard patterns.
    """
    if host in rules:
        return rules[host]
    for pattern, headers in rules.items():
        if "*" in pattern and fnmatch.fnmatch(host, pattern):
            return headers
    return None


class CredentialInjector:
    """mitmproxy addon that injects headers based on domain-matching rules."""

    def __init__(self) -> None:
        self._rules_path = os.environ.get("FLINT_PROXY_RULES", "")
        self._rules: dict = {}
        self._mtime: float = 0

    def _reload(self) -> None:
        """Reload rules if the file has changed."""
        try:
            mtime = os.path.getmtime(self._rules_path)
            if mtime != self._mtime:
                self._rules = _load_rules(self._rules_path)
                self._mtime = mtime
        except OSError:
            pass

    def request(self, flow: http.HTTPFlow) -> None:
        """Intercept requests and inject headers for matching domains."""
        self._reload()

        host = flow.request.pretty_host  # works correctly in transparent mode
        headers_to_inject = _match_domain(host, self._rules)
        if not headers_to_inject:
            return

        # Overwrite any existing headers with the same name (security requirement:
        # prevents sandbox code from substituting its own credentials)
        for name, value in headers_to_inject.items():
            flow.request.headers[name] = value


addons = [CredentialInjector()]

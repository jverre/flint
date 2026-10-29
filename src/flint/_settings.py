"""Settings shared by the daemon and its clients (SDK, CLI, TUI), from the environment.

Kept free of side effects and heavy imports: the SDK reads where the daemon
is from here, so importing it never loads the VM manager (``flint.core``).
"""

from __future__ import annotations

import json
import os
import platform
import time


def default_dirs() -> tuple[str, str]:
    """The default (data, state) directories on this platform."""
    if platform.system() == "Darwin":
        base = os.path.expanduser("~/Library/Application Support/flint")
        return f"{base}/data", f"{base}/state"
    return "/var/lib/flint/data", "/var/lib/flint/state"


DAEMON_HOST = os.environ.get("FLINT_HOST", "127.0.0.1")
DAEMON_PORT = int(os.environ.get("FLINT_PORT", "9100"))
STATE_DIR = os.environ.get("FLINT_STATE_DIR", default_dirs()[1])

# The daemon's Unix socket. Whoever can open it is an admin, so it is only
# accessible to the daemon's user (and the `flint` group, if there is one).
# TCP needs an API token.
DAEMON_SOCKET = os.environ.get("FLINT_SOCKET", f"{STATE_DIR}/flint.sock")
# Where clients (SDK, CLI, TUI) send requests: the local daemon's socket by
# default, a daemon over TCP (FLINT_URL=http://127.0.0.1:9100 with
# FLINT_API_KEY), or a cluster's control plane (FLINT_URL=https://...).
DAEMON_URL = os.environ.get("FLINT_URL", f"unix://{DAEMON_SOCKET}").rstrip("/")
# `flint login` keeps sign-in tokens here, per server URL (0600).
CREDENTIALS_PATH = os.environ.get("FLINT_CREDENTIALS", os.path.expanduser("~/.config/flint/credentials.json"))


def stored_token(url: str) -> str:
    """The unexpired token `flint login` stored for ``url``, or ""."""
    try:
        with open(CREDENTIALS_PATH) as f:
            cred = json.load(f).get(url.rstrip("/")) or {}
    except (OSError, ValueError):
        return ""
    return cred.get("token", "") if (cred.get("expires_at") or float("inf")) > time.time() else ""


API_KEY = os.environ.get("FLINT_API_KEY", "") or stored_token(DAEMON_URL)
TENANT = os.environ.get("FLINT_TENANT", "")

DEFAULT_TEMPLATE_ID = "default"

# The interactive terminal's size.
TERM_COLS = 120
TERM_ROWS = 40

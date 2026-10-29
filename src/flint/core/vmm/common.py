"""Pieces every Linux VMM driver uses: process handles, timing, log tails, waiting for the guest agent."""

from __future__ import annotations

import os
import signal
import subprocess
import time
import urllib.request

from ..config import AGENT_PORT, GUEST_IP
from ..net.netns import _enter_netns, _restore_netns

AGENT_URL = f"http://{GUEST_IP}:{AGENT_PORT}"


class RecoveredProcess:
    """Process handle for a VMM adopted after a daemon restart (not our child)."""

    def __init__(self, pid: int) -> None:
        self.pid = pid

    def kill(self) -> None:
        try:
            os.kill(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def wait(self, timeout: float | None = None) -> None:
        deadline = time.monotonic() + (timeout or 10)
        while time.monotonic() < deadline:
            try:
                os.kill(self.pid, 0)
                time.sleep(0.1)
            except ProcessLookupError:
                return


def stop_process(process, timeout: float = 2) -> None:
    """SIGKILL a VMM and reap it; best effort, it may be gone already."""
    if process is None:
        return
    try:
        process.kill()
    except (ProcessLookupError, OSError):
        pass
    try:
        process.wait(timeout=timeout)
    except (subprocess.TimeoutExpired, ChildProcessError, OSError):
        pass


def pid_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def timed(timings: dict, key: str):
    """Context manager that records how long its block took, in ms, as ``timings[key]``."""

    class _Timer:
        def __enter__(self):
            self.t0 = time.monotonic()
            return self

        def __exit__(self, *_):
            timings[key] = (time.monotonic() - self.t0) * 1000

    return _Timer()


def log_tail(path: str | None, nbytes: int = 2000) -> str:
    """The last ``nbytes`` of a log file, without reading all of it ("" if unreadable)."""
    if not path:
        return ""
    try:
        with open(path, "rb") as f:
            try:
                f.seek(-nbytes, os.SEEK_END)
            except OSError:
                f.seek(0)
            return f.read().decode(errors="replace")
    except OSError:
        return ""


def wait_for_agent(ns_name: str, retries: int = 500) -> str:
    """Wait for the guest agent in ``ns_name`` to answer /health. Returns its URL."""
    health_url = f"{AGENT_URL}/health"
    orig_fd = _enter_netns(ns_name)
    try:
        for _ in range(retries):
            try:
                req = urllib.request.Request(health_url, method="GET")
                with urllib.request.urlopen(req, timeout=0.1) as resp:
                    if resp.status == 200:
                        return AGENT_URL
            except Exception:
                time.sleep(0.001)
        raise TimeoutError(f"Agent health check failed after {retries} attempts")
    finally:
        _restore_netns(orig_fd)

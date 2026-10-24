"""Observed maintenance state. Never infer a running VMM from its install path."""

from __future__ import annotations

import functools
import hashlib
import json
import os
import platform
from pathlib import Path
import subprocess
import time
import threading


def command_json(argv: list[str]) -> dict:
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=3, check=True
        )
        return {"status": "available", "data": json.loads(result.stdout)}
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return {"status": "unknown", "error": type(exc).__name__}


@functools.lru_cache(maxsize=256)
def _binary_identity(path: str, device: int, inode: int, size: int, mtime: int) -> dict:
    # Cache by file identity, not pathname. A replaced install path cannot
    # change the result for a process still executing the old inode.
    try:
        with open(path, "rb") as source:
            st = os.fstat(source.fileno())
            if (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns) != (
                device,
                inode,
                size,
                mtime,
            ):
                return {"status": "unknown"}
            digest = hashlib.file_digest(source, "sha256").hexdigest()
            # A compromised VMM could exec a guest-owned file. Never run that
            # file as root while trying to discover its version.
            if path.startswith("/proc/") and (st.st_uid != 0 or st.st_mode & 0o022):
                return {"status": "untrusted", "sha256": digest}
            fd_path = (
                f"/proc/self/fd/{source.fileno()}"
                if platform.system() == "Linux"
                else f"/dev/fd/{source.fileno()}"
            )
            result = subprocess.run(
                [fd_path, "--version"],
                pass_fds=(source.fileno(),),
                capture_output=True,
                text=True,
                timeout=3,
                check=True,
            )
            return {
                "status": "observed",
                "version": result.stdout.strip()[:512],
                "sha256": digest,
            }
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "unknown", "error": type(exc).__name__}


def binary_identity(path: str) -> dict:
    try:
        st = os.stat(path)
        result = dict(
            _binary_identity(path, st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
        )
        current = os.stat(path)
        if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (
            st.st_dev,
            st.st_ino,
            st.st_size,
            st.st_mtime_ns,
        ):
            return {"status": "unknown"}
        return result
    except OSError:
        return {"status": "unknown"}


class PatchInventory:
    def __init__(self) -> None:
        self._at = 0.0
        self._host: dict = {}
        self._result: dict = {"status": "collecting"}
        self._lock = threading.Lock()
        self._collecting = False

    def report(self, binary: str, processes: dict[str, int]) -> dict:
        # Hashing and vendor tools must never delay lease renewal.
        with self._lock:
            if not self._collecting and (
                not self._at or time.monotonic() - self._at >= 60
            ):
                self._collecting = True
                threading.Thread(
                    target=self._collect,
                    args=(binary, dict(processes)),
                    daemon=True,
                    name="patch-inventory",
                ).start()
            return dict(self._result)

    def _collect(self, binary: str, processes: dict[str, int]) -> None:
        try:
            result = self._sample(binary, processes)
            with self._lock:
                self._result = result
                self._at = time.monotonic()
        finally:
            with self._lock:
                self._collecting = False

    def _sample(self, binary: str, processes: dict[str, int]) -> dict:
        # Livepatch queries can be slow; do not put them on every heartbeat.
        if not self._host or time.monotonic() - self._at >= 60:
            patches = []
            try:
                for patch in sorted(Path("/sys/kernel/livepatch").iterdir()):
                    patches.append(
                        {
                            "name": patch.name,
                            "enabled": (patch / "enabled").read_text().strip() == "1",
                        }
                    )
                kernel_patches = {"status": "observed", "patches": patches}
            except OSError:
                kernel_patches = {"status": "unknown", "patches": []}
            self._host = {
                "running_kernel": platform.release(),
                "kernel_livepatch": kernel_patches,
                "canonical_livepatch": command_json(
                    ["canonical-livepatch", "status", "--format=json"]
                ),
                "reboot_required": Path("/var/run/reboot-required").exists()
                if platform.system() == "Linux"
                else None,
                "observed_at": time.time(),
                "host_image": os.environ.get("FLINT_HOST_IMAGE", "unknown"),
                "expected_kernel": os.environ.get("FLINT_HOST_KERNEL", ""),
                "rollout_group": os.environ.get("FLINT_ROLLOUT_GROUP", "stable"),
            }
        return {
            **self._host,
            "installed_vmm": binary_identity(binary),
            "running_vmms": {
                vm: {"pid": pid, **binary_identity(f"/proc/{pid}/exe")}
                for vm, pid in processes.items()
            },
        }

from __future__ import annotations

import platform

from .base import HostBackend


def get_host_backend() -> HostBackend:
    system = platform.system()
    machine = platform.machine().lower()

    if system == "Linux":
        from .linux_firecracker import LinuxFirecrackerBackend

        return LinuxFirecrackerBackend()

    if system == "Darwin" and machine == "arm64":
        from .macos_vz import MacOSVirtualizationBackend

        return MacOSVirtualizationBackend()

    raise RuntimeError(
        f"Unsupported host: {system} {machine}. "
        "Flint supports Linux hosts with Firecracker and Apple Silicon macOS hosts with Virtualization.framework."
    )


"""Cloud-Hypervisor backend plugin.

Boots micro-VMs via the ``cloud-hypervisor`` binary using the REST API exposed
on a Unix domain socket. Reuses the same ``br-flint`` bridge + netns + TAP
infrastructure as the Firecracker backend; isolation uses CH's built-in
``--seccomp true`` and a ``systemd-run`` cgroup slice (no jailer).

This plugin is still WIP — the lifecycle methods raise
:class:`NotImplementedError` until the boot/snapshot pipeline lands. The
surface that *is* implemented (registration, preflight, ``backends list``,
template artifact validation, install hook) is sufficient for the plugin
system to route users to CH without tripping up FC.
"""

from __future__ import annotations

import os
import platform
import shutil

from flint.core.config import (
    CH_BINARY,
    CH_GOLDEN_DIR,
    log,
)
from flint.core.types import _SandboxEntry

from .base import BackendBootResult, HostBackend


class LinuxCloudHypervisorBackend(HostBackend):
    name = "cloud-hypervisor"
    kind = "linux-cloud-hypervisor"
    display_name = "Cloud-Hypervisor (Linux)"
    supported_platforms = ("linux",)

    def preflight(self) -> list[str]:
        problems = super().preflight()
        if problems:
            return problems
        if not os.path.exists(CH_BINARY) and not shutil.which("cloud-hypervisor"):
            problems.append(
                f"cloud-hypervisor binary not found at {CH_BINARY} or on PATH"
            )
        return problems

    def template_artifact_valid(self, template_dir: str) -> bool:
        required = ("rootfs.ext4", "config.json", "state.json", "memory-ranges")
        return all(os.path.exists(os.path.join(template_dir, f)) for f in required)

    def install_dependencies(self, **kwargs) -> None:
        install_dir = kwargs.get("install_dir", "/usr/local/bin")
        version = kwargs.get("version", "latest")
        _install_ch_binary(install_dir=install_dir, version=version)

    def ensure_runtime_ready(self) -> None:
        from flint.core._netns import _ensure_bridge

        _ensure_bridge()

    def ensure_default_template(self) -> None:
        os.makedirs(CH_GOLDEN_DIR, exist_ok=True)
        log.info(
            "cloud-hypervisor default template is not yet auto-generated "
            "(CH_GOLDEN_DIR=%s)",
            CH_GOLDEN_DIR,
        )

    def start_pool(self) -> None:
        pass

    def stop_pool(self) -> None:
        pass

    def create(
        self,
        *,
        template_id: str,
        allow_internet_access: bool,
        use_pool: bool,
        use_pyroute2: bool,
    ) -> BackendBootResult:
        raise NotImplementedError(
            "cloud-hypervisor backend lifecycle is not yet implemented; "
            "the plugin system is in place but the boot/snapshot pipeline is a follow-up"
        )

    def kill(self, entry: _SandboxEntry) -> None:
        pid = getattr(entry, "pid", 0)
        if pid:
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass

    def pause(self, entry: _SandboxEntry, state_store) -> None:
        raise NotImplementedError("cloud-hypervisor pause not yet implemented")

    def resume(self, row: dict) -> BackendBootResult:
        raise NotImplementedError("cloud-hypervisor resume not yet implemented")

    def proxy_guest_request(
        self,
        entry,
        method: str,
        path: str,
        body: bytes | None = None,
        timeout: float = 65,
    ) -> tuple[int, bytes]:
        raise NotImplementedError("cloud-hypervisor proxy not yet implemented")

    async def bridge_terminal(self, entry, websocket) -> None:
        raise NotImplementedError("cloud-hypervisor terminal bridge not yet implemented")

    def check_entry_alive(self, entry) -> tuple[bool, str | None]:
        pid = getattr(entry, "pid", 0)
        if not pid:
            return False, "no pid recorded"
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False, f"process {pid} not found"
        except PermissionError:
            pass
        return True, None

    def recover_row(self, row: dict):
        return "dead", None

    def build_template(self, name: str, dockerfile: str, rootfs_size_mb: int = 500) -> dict:
        raise NotImplementedError(
            "cloud-hypervisor build_template is not yet implemented"
        )

    def delete_template_artifact(self, template_id: str, template: dict | None = None) -> None:
        from flint.core._template_registry import delete_template_artifact as _delete_template_artifact

        _delete_template_artifact(template_id, self.kind)


def _install_ch_binary(*, install_dir: str, version: str) -> None:
    """Download the cloud-hypervisor binary release into ``install_dir``.

    Uses the GitHub release asset for the current host architecture. Raises on
    failure; caller can catch and report.
    """
    import subprocess
    import urllib.request

    arch = platform.machine().lower()
    if arch in ("x86_64", "amd64"):
        asset_arch = "x86_64"
    elif arch in ("aarch64", "arm64"):
        asset_arch = "aarch64"
    else:
        raise RuntimeError(f"cloud-hypervisor has no release asset for {arch!r}")

    if version == "latest":
        # Resolve the latest release tag
        with urllib.request.urlopen(
            "https://api.github.com/repos/cloud-hypervisor/cloud-hypervisor/releases/latest",
            timeout=30,
        ) as resp:
            import json as _json
            tag = _json.load(resp)["tag_name"]
    else:
        tag = version if version.startswith("v") else f"v{version}"

    url = (
        f"https://github.com/cloud-hypervisor/cloud-hypervisor/releases/"
        f"download/{tag}/cloud-hypervisor-static"
        f"{'' if asset_arch == 'x86_64' else '-aarch64'}"
    )
    os.makedirs(install_dir, exist_ok=True)
    target = os.path.join(install_dir, "cloud-hypervisor")
    log.info("Downloading cloud-hypervisor %s from %s -> %s", tag, url, target)
    urllib.request.urlretrieve(url, target)
    os.chmod(target, 0o755)
    # Quick sanity check
    subprocess.run([target, "--version"], check=True, capture_output=True)


from .registry import register as _register

_register(LinuxCloudHypervisorBackend)

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class BackendBootResult:
    process: Any = None
    pid: int = -1
    vm_dir: str = ""
    socket_path: str = ""
    ns_name: str = ""
    guest_ip: str = ""
    agent_url: str = ""
    chroot_base: str = ""
    backend_vm_ref: str = ""
    runtime_dir: str = ""
    guest_arch: str = ""
    transport_ref: str = ""
    timings: dict[str, float] = field(default_factory=dict)
    t_total: float = 0.0
    backend_metadata: dict[str, Any] = field(default_factory=dict)


class HostBackend(ABC):
    kind: str

    @abstractmethod
    def ensure_runtime_ready(self) -> None:
        pass

    @abstractmethod
    def ensure_default_template(self) -> None:
        pass

    @abstractmethod
    def start_pool(self) -> None:
        pass

    @abstractmethod
    def stop_pool(self) -> None:
        pass

    @abstractmethod
    def create(
        self,
        *,
        template_id: str,
        allow_internet_access: bool,
        use_pool: bool,
        use_pyroute2: bool,
    ) -> BackendBootResult:
        pass

    @abstractmethod
    def kill(self, entry) -> None:
        pass

    @abstractmethod
    def pause(self, entry, state_store) -> None:
        pass

    @abstractmethod
    def resume(self, row: dict) -> BackendBootResult:
        pass

    @abstractmethod
    def proxy_guest_request(
        self,
        entry,
        method: str,
        path: str,
        body: bytes | None = None,
        timeout: float = 65,
    ) -> tuple[int, bytes]:
        pass

    @abstractmethod
    async def bridge_terminal(self, entry, websocket) -> None:
        pass

    @abstractmethod
    def check_entry_alive(self, entry) -> tuple[bool, str | None]:
        pass

    @abstractmethod
    def recover_row(self, row: dict):
        """Return ('alive' | 'paused' | 'dead', entry_or_none)."""
        pass

    @abstractmethod
    def build_template(self, name: str, dockerfile: str, rootfs_size_mb: int = 500) -> dict:
        pass

    @abstractmethod
    def delete_template_artifact(self, template_id: str, template: dict | None = None) -> None:
        pass


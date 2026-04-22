"""Single source of truth for the TUI's shared state.

Owned by `FlintApp`; mutated exclusively by the events worker. Widgets read
from here and react to typed messages posted by the app when state changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AppState:
    sandboxes: dict[str, dict] = field(default_factory=dict)
    volumes: list[dict] = field(default_factory=list)
    templates: list[dict] = field(default_factory=list)
    agents: list[dict] = field(default_factory=list)
    config: dict = field(default_factory=dict)

    # UI state
    selected_vm_id: str | None = None
    selected_ids: set[str] = field(default_factory=set)
    filter_text: str = ""
    sort_key: str = "created"  # "created" | "ready" | "state" | "name"
    ws_connected: bool = False

    def filtered_sorted_sandboxes(self) -> list[dict]:
        vms = list(self.sandboxes.values())
        if self.filter_text:
            needle = self.filter_text.lower()
            vms = [
                v for v in vms
                if needle in v.get("vm_id", "").lower()
                or needle in v.get("state", "").lower()
                or needle in v.get("template_id", "").lower()
            ]
        key = self.sort_key
        if key == "name":
            vms.sort(key=lambda v: v.get("vm_id", ""))
        elif key == "state":
            vms.sort(key=lambda v: v.get("state", ""))
        elif key == "ready":
            vms.sort(key=lambda v: v.get("ready_time_ms") or 0)
        else:
            vms.sort(key=lambda v: v.get("created_at", 0))
        return vms

"""Sidebar widget for VM list"""
import time

from textual.app import ComposeResult
from textual.containers import Container
from textual.events import Click
from textual.reactive import reactive
from textual.widgets import Static, ListView, ListItem, Label

from flint.sandbox import Sandbox


def _relative_time(created_at: float) -> str:
    """Return a human-readable relative time string."""
    delta = time.time() - created_at
    if delta < 10:
        return "just now"
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


class Sidebar(Container):
    """Sidebar displaying list of VMs"""
    DEFAULT_CSS = """
    Sidebar {
        width: 1fr;
        height: auto;
        max-height: 50%;
    }

    #vm-list {
        height: auto;
    }

    #vm-list > ListItem {
        height: 1;
        padding: 0 1;
    }

    #vm-list > ListItem:hover {
        background: $primary 10%;
    }

    #vm-list > ListItem.-highlight {
        background: $primary 15%;
    }
    """

    vm_count = reactive(0)

    def compose(self) -> ComposeResult:
        yield ListView(id="vm-list")

    def on_click(self, event: Click) -> None:
        self.query_one("#vm-list", ListView).focus()

    def on_mount(self) -> None:
        self._last_snapshot: list[tuple[str, str]] = []
        self._vm_created: dict[str, float] = {}
        self.set_interval(0.5, self._refresh_list)

    def watch_vm_count(self) -> None:
        self._refresh_list()

    @staticmethod
    def _state_dot(state: str) -> str:
        if state == "Started":
            return "[green]\u25cf[/]"
        elif state == "Error":
            return "[red]\u25cf[/]"
        else:
            return "[dim]\u25cb[/]"

    def _make_label(self, short_id: str, state: str, created_at: float) -> str:
        dot = self._state_dot(state)
        age = _relative_time(created_at)
        # Right-align the age by padding
        name_part = f"{dot} {short_id}"
        return f"{name_part}    [dim]{age}[/]"

    def _refresh_list(self) -> None:
        try:
            sandboxes = Sandbox.list()
        except Exception:
            return

        # Fetch created_at for each VM
        vm_data: list[tuple[str, str, float]] = []
        for sb in sandboxes:
            data = sb._fetch()
            if data:
                created = data.get("created_at", 0.0)
                state = data.get("state", "Starting")
                vm_data.append((sb.id, state, created))

        snapshot = [(vid, st) for vid, st, _ in vm_data]
        created_map = {vid: cr for vid, _, cr in vm_data}

        if snapshot == self._last_snapshot:
            # Still update time labels
            self._update_time_labels(created_map)
            return
        self._last_snapshot = snapshot
        self._vm_created = created_map

        # Update section title via screen
        try:
            from flint.tui.screens.home import HomeScreen
            screen = self.screen
            if isinstance(screen, HomeScreen):
                screen._update_section_title(len(vm_data))
        except Exception:
            pass

        vm_list = self.query_one("#vm-list", ListView)
        current_ids = [getattr(child, "vm_id", None) for child in vm_list.children]
        new_ids = [vid for vid, _, _ in vm_data]
        data_dict = {vid: (st, cr) for vid, st, cr in vm_data}

        # Remove items no longer present
        for i in range(len(current_ids) - 1, -1, -1):
            if current_ids[i] not in data_dict:
                vm_list.pop(i)

        # Update existing items, append new ones
        existing = {getattr(child, "vm_id", None): child for child in vm_list.children}
        for vm_id in new_ids:
            state, created = data_dict[vm_id]
            short_id = vm_id[:8]
            label_text = self._make_label(short_id, state, created)
            if vm_id in existing:
                existing[vm_id].query_one(Label).update(label_text)
            else:
                item = ListItem(Label(label_text))
                item.vm_id = vm_id
                vm_list.append(item)

    def _update_time_labels(self, created_map: dict[str, float]) -> None:
        """Update just the time labels without rebuilding the list."""
        vm_list = self.query_one("#vm-list", ListView)
        for child in vm_list.children:
            vm_id = getattr(child, "vm_id", None)
            if vm_id and vm_id in created_map:
                state = dict(self._last_snapshot).get(vm_id, "Starting")
                short_id = vm_id[:8]
                label_text = self._make_label(short_id, state, created_map[vm_id])
                child.query_one(Label).update(label_text)

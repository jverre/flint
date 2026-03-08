"""Sidebar widget for VM list"""
from textual.app import ComposeResult
from textual.containers import Container
from textual.events import Click
from textual.reactive import reactive
from textual.widgets import Static, ListView, ListItem, Label

from flint.sandbox import Sandbox


class Sidebar(Container):
    """Sidebar displaying list of VMs"""
    DEFAULT_CSS = """
    Sidebar {
        width: 40;
        background: $panel;
        border-right: tall $primary 30%;
    }

    .sidebar-title {
        height: 1;
        padding: 0 1;
        background: $primary 10%;
        color: $text;
        text-style: bold;
    }

    #vm-count-label {
        height: 1;
        padding: 0 1;
        color: $text-muted;
        text-style: dim;
    }

    #vm-list {
        height: 1fr;
    }

    #vm-list > ListItem {
        height: 1;
        padding: 0 1;
    }

    #vm-list > ListItem:hover {
        background: $primary 15%;
    }

    #vm-list > ListItem.-highlight {
        background: $primary 25%;
    }
    """

    vm_count = reactive(0)

    def compose(self) -> ComposeResult:
        yield Static("Virtual Machines", classes="sidebar-title")
        yield Static("0 running", id="vm-count-label")
        yield ListView(id="vm-list")

    def on_click(self, event: Click) -> None:
        self.query_one("#vm-list", ListView).focus()

    def on_mount(self) -> None:
        self._last_snapshot: list[tuple[str, str]] = []
        self.set_interval(0.5, self._refresh_list)

    def watch_vm_count(self) -> None:
        self._refresh_list()

    @staticmethod
    def _make_label(short_id: str, state: str) -> str:
        if state == "Started":
            badge = "[green]\u25cf[/] Running"
        elif state == "Error":
            badge = "[red]\u25cf[/] Error"
        else:
            badge = "[yellow]\u25cb[/] Starting"
        return f" {short_id}   {badge}"

    def _refresh_list(self) -> None:
        try:
            sandboxes = Sandbox.list()
        except Exception:
            return
        snapshot = [(sb.id, sb.state) for sb in sandboxes]
        if snapshot == self._last_snapshot:
            return
        self._last_snapshot = snapshot
        self.query_one("#vm-count-label", Static).update(
            f"{self.vm_count} running"
        )

        vm_list = self.query_one("#vm-list", ListView)
        current_ids = [getattr(child, "vm_id", None) for child in vm_list.children]
        new_ids = [vid for vid, _ in snapshot]
        snapshot_dict = dict(snapshot)

        # Remove items no longer present (iterate in reverse to keep indices stable)
        for i in range(len(current_ids) - 1, -1, -1):
            if current_ids[i] not in snapshot_dict:
                vm_list.pop(i)

        # Update existing items in place, append new ones
        existing = {getattr(child, "vm_id", None): child for child in vm_list.children}
        for vm_id in new_ids:
            state = snapshot_dict[vm_id]
            short_id = vm_id[:8]
            label_text = self._make_label(short_id, state)
            if vm_id in existing:
                existing[vm_id].query_one(Label).update(label_text)
            else:
                item = ListItem(Label(label_text))
                item.vm_id = vm_id
                vm_list.append(item)

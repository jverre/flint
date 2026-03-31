"""Sidebar widget for VM list"""

from time import time

from textual.app import ComposeResult
from textual.containers import Container, Horizontal
from textual.events import Click
from textual.reactive import reactive
from textual.widgets import Static, ListView, ListItem, Label

from flint.sandbox import Sandbox


def _relative_time(created_at: float) -> str:
    """Format a timestamp as a human-readable relative time."""
    if created_at <= 0:
        return ""
    delta = int(time() - created_at)
    if delta < 60:
        return "just now"
    if delta < 3600:
        m = delta // 60
        return f"{m}m ago"
    if delta < 86400:
        h = delta // 3600
        return f"{h}hr ago"
    d = delta // 86400
    return f"{d}d ago"


class Sidebar(Container):
    """Sidebar displaying list of VMs"""

    DEFAULT_CSS = """
    Sidebar {
        width: 40;
        background: $panel;
        border-right: tall $border;
    }

    /* ── Brand header ─────────────────────────────── */

    #sidebar-brand {
        height: 3;
        padding: 1 2;
        text-style: bold;
        color: $primary;
        content-align-vertical: middle;
    }

    /* ── Section header ───────────────────────────── */

    #section-header {
        height: 1;
        padding: 0 2;
    }

    #section-title {
        width: 1fr;
        color: $text-muted;
        text-style: bold;
    }

    #btn-new-vm {
        width: 3;
        color: $primary;
        text-style: bold;
        text-align: center;
    }

    #btn-new-vm:hover {
        color: $text;
        background: $primary 20%;
    }

    /* ── VM list ──────────────────────────────────── */

    #vm-list {
        height: 1fr;
        margin-top: 1;
    }

    #vm-list > ListItem {
        height: 1;
        padding: 0 2;
    }

    #vm-list > ListItem:hover {
        background: $primary 10%;
    }

    #vm-list > ListItem.-highlight {
        background: $primary 20%;
    }

    .vm-row {
        width: 100%;
        height: 1;
    }

    .vm-name {
        width: 1fr;
        color: $text;
    }

    .vm-time {
        width: auto;
        color: $text-muted;
        text-style: italic;
    }
    """

    vm_count = reactive(0)

    def compose(self) -> ComposeResult:
        yield Static("Flint", id="sidebar-brand")
        with Horizontal(id="section-header"):
            yield Static("Virtual Machines", id="section-title")
            yield Static(" + ", id="btn-new-vm")
        yield ListView(id="vm-list")

    def on_click(self, event: Click) -> None:
        # Click on + button triggers new VM
        try:
            btn = self.query_one("#btn-new-vm")
            if event.widget is btn:
                self.screen.action_start_vm()
                return
        except Exception:
            pass
        self.query_one("#vm-list", ListView).focus()

    def on_mount(self) -> None:
        self._last_snapshot: list[tuple[str, str, float]] = []
        self.set_interval(0.5, self._refresh_list)

    def watch_vm_count(self) -> None:
        self._refresh_list()

    @staticmethod
    def _make_label(short_id: str, state: str) -> str:
        if state == "Started":
            badge = "[green]\u25cf[/]"
        elif state == "Error":
            badge = "[red]\u25cf[/]"
        else:
            badge = "[yellow]\u25cb[/]"
        return f" {badge} {short_id}"

    def _refresh_list(self) -> None:
        try:
            sandboxes = Sandbox.list()
        except Exception:
            return
        snapshot = [(sb.id, sb.state, sb.created_at) for sb in sandboxes]
        if snapshot == self._last_snapshot:
            return
        self._last_snapshot = snapshot
        self.query_one("#section-title", Static).update(
            f"Virtual Machines ({self.vm_count})"
        )

        vm_list = self.query_one("#vm-list", ListView)
        current_ids = [getattr(child, "vm_id", None) for child in vm_list.children]
        new_ids = [vid for vid, _, _ in snapshot]
        snapshot_dict = {vid: (state, created) for vid, state, created in snapshot}

        # Remove items no longer present (iterate in reverse to keep indices stable)
        for i in range(len(current_ids) - 1, -1, -1):
            if current_ids[i] not in snapshot_dict:
                vm_list.pop(i)

        # Update existing items in place, append new ones
        existing = {getattr(child, "vm_id", None): child for child in vm_list.children}
        for vm_id in new_ids:
            state, created = snapshot_dict[vm_id]
            short_id = vm_id[:8]
            label_text = self._make_label(short_id, state)
            time_text = _relative_time(created)
            if vm_id in existing:
                item = existing[vm_id]
                item.query_one(".vm-name", Label).update(label_text)
                item.query_one(".vm-time", Label).update(time_text)
            else:
                item = ListItem(
                    Horizontal(
                        Label(label_text, classes="vm-name"),
                        Label(time_text, classes="vm-time"),
                        classes="vm-row",
                    )
                )
                item.vm_id = vm_id
                vm_list.append(item)

"""Sidebar widget for VM list.

Event-driven: reads from ``app.state.sandboxes`` and reacts to
``SandboxesChanged`` messages. A 1-second timer refreshes the relative-time
labels only (no network I/O).
"""

from __future__ import annotations

import time

from textual.app import ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.events import Click
from textual.reactive import reactive
from textual.widgets import Static, Button, ListView, ListItem, Label, Input

from flint.tui.events import SandboxesChanged, WsStateChanged
from flint.tui.palette import ERROR_HEX, MUTED_HEX, SUCCESS_HEX, WARNING_HEX


def _relative_time(created_at: float) -> str:
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
    DEFAULT_CSS = """
    Sidebar {
        width: 32;
        height: 1fr;
        padding: 0;
        background: $surface;
        border-right: solid $border;
    }

    #sidebar-top { height: auto; padding: 1 0; background: $surface; }
    #brand-header { height: 1; color: $text-muted; padding: 0 1; background: transparent; }
    #section-header { height: 1; padding: 0 1; background: transparent; }
    #section-title { width: 1fr; height: 1; color: $text-muted; }

    #new-vm-btn {
        min-width: 5; height: 1; border: none; padding: 0 1;
        background: transparent; color: $text-muted; dock: right;
    }
    #new-vm-btn:hover { color: $text; }

    #sidebar-filter {
        height: 1; padding: 0 1; margin: 0 1; border: none; background: transparent; color: $text;
    }
    #sidebar-filter:focus { background: $primary 12%; }

    #vm-list { height: 1fr; padding: 0; background: transparent; }
    #vm-list > ListItem { height: 1; padding: 0 1; background: transparent; color: $text-muted; }
    #vm-list > ListItem:hover { background: $primary 10%; color: $text; }
    #vm-list > ListItem.-highlight { background: $primary 18%; color: $text; }
    #vm-list:focus > ListItem.-highlight { background: $primary 24%; color: $text; }

    #sidebar-footer { dock: bottom; height: auto; padding: 1 0; background: transparent; }
    .sidebar-footer-item { height: 1; padding: 0 1; background: transparent; color: $text-muted; }
    #benchmark-btn, #keybindings-btn { color: $text-muted; }
    #benchmark-btn:hover, #keybindings-btn:hover { color: $text; }
    """

    vm_count = reactive(0)
    _LABEL_WIDTH = 27  # sidebar 32 - 1 border - 2 padding - selection prefix space

    def compose(self) -> ComposeResult:
        with Vertical(id="sidebar-top"):
            yield Static("Flint", id="brand-header")
            with Horizontal(id="section-header"):
                yield Static("Virtual Machines (0)", id="section-title")
                yield Button("+ VM", id="new-vm-btn", variant="default")
            yield Input(placeholder="filter…", id="sidebar-filter")
        yield ListView(id="vm-list")
        with Vertical(id="sidebar-footer"):
            yield Static(
                f"Benchmark [{WARNING_HEX}](ctrl+b)[/]",
                id="benchmark-btn",
                classes="sidebar-footer-item",
            )
            yield Static(
                f"Keybindings [{WARNING_HEX}](?)[/]",
                id="keybindings-btn",
                classes="sidebar-footer-item",
            )
            yield Static(
                f"[{MUTED_HEX}]ws: connecting…[/]",
                id="ws-status",
                classes="sidebar-footer-item",
            )

    def on_mount(self) -> None:
        self._last_signature: list[tuple] = []
        # Cheap relative-time refresh only.
        self.set_interval(1.0, self._refresh_time_labels)
        self._rebuild()

    def on_click(self, event: Click) -> None:
        widget = event.widget
        if widget.id == "keybindings-btn" or any(a.id == "keybindings-btn" for a in widget.ancestors):
            self.app.push_screen("keybindings")
            return
        if widget.id == "benchmark-btn" or any(a.id == "benchmark-btn" for a in widget.ancestors):
            self.app.push_screen("benchmark")
            return
        self.query_one("#vm-list", ListView).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "sidebar-filter":
            self.app.state.filter_text = event.value or ""
            self._rebuild()

    def on_sandboxes_changed(self, event: SandboxesChanged) -> None:
        self._rebuild()

    def on_ws_state_changed(self, event: WsStateChanged) -> None:
        try:
            label = self.query_one("#ws-status", Static)
        except Exception:
            return
        if event.connected:
            label.update(f"[{SUCCESS_HEX}]ws: connected[/]")
        else:
            label.update(f"[{ERROR_HEX}]ws: reconnecting…[/]")

    def watch_vm_count(self) -> None:
        self._rebuild()

    # ── Rendering ──────────────────────────────────────────────────────────

    @staticmethod
    def _state_dot(state: str) -> str:
        if state == "Started" or state == "Running":
            return f"[{SUCCESS_HEX}]●[/]"
        if state == "Error":
            return f"[{ERROR_HEX}]●[/]"
        if state == "Starting":
            return f"[{WARNING_HEX}]●[/]"
        return "[dim]○[/]"

    def _make_label(self, vm_id: str, state: str, created_at: float, selected: bool) -> str:
        dot = self._state_dot(state)
        short_id = vm_id[:8]
        age = _relative_time(created_at)
        prefix = "[b]✓[/] " if selected else "  "
        name_part = f"{prefix}{dot} {short_id}"
        visible_name = 2 + 1 + 1 + len(short_id)
        padding = self._LABEL_WIDTH - visible_name - len(age)
        return f"{name_part}{' ' * max(padding, 1)}[{MUTED_HEX}]{age}[/]"

    def _rebuild(self) -> None:
        state = getattr(self.app, "state", None)
        if state is None:
            return
        vms = state.filtered_sorted_sandboxes()
        self.vm_count = len(vms)

        # Update section title via Static directly (avoids screen-type coupling).
        try:
            title = self.query_one("#section-title", Static)
            title.update(f"Virtual Machines ({len(vms)})")
        except Exception:
            pass

        signature = [(v.get("vm_id"), v.get("state"), v.get("created_at", 0), v.get("vm_id") in state.selected_ids) for v in vms]
        if signature == self._last_signature:
            return
        self._last_signature = signature

        vm_list = self.query_one("#vm-list", ListView)
        current_ids = [getattr(child, "vm_id", None) for child in vm_list.children]
        new_ids = [v.get("vm_id") for v in vms]

        for i in range(len(current_ids) - 1, -1, -1):
            if current_ids[i] not in new_ids:
                vm_list.pop(i)

        existing = {getattr(child, "vm_id", None): child for child in vm_list.children}
        for i, vm in enumerate(vms):
            vm_id = vm.get("vm_id")
            if not vm_id:
                continue
            label_text = self._make_label(
                vm_id,
                vm.get("state", "Starting"),
                vm.get("created_at", 0),
                vm_id in state.selected_ids,
            )
            if vm_id in existing:
                existing[vm_id].query_one(Label).update(label_text)
            else:
                item = ListItem(Label(label_text))
                item.vm_id = vm_id
                vm_list.append(item)

    def _refresh_time_labels(self) -> None:
        state = getattr(self.app, "state", None)
        if state is None:
            return
        vms = state.filtered_sorted_sandboxes()
        by_id = {v.get("vm_id"): v for v in vms}
        vm_list = self.query_one("#vm-list", ListView)
        for child in vm_list.children:
            vm_id = getattr(child, "vm_id", None)
            vm = by_id.get(vm_id)
            if not vm:
                continue
            label_text = self._make_label(
                vm_id,
                vm.get("state", "Starting"),
                vm.get("created_at", 0),
                vm_id in state.selected_ids,
            )
            try:
                child.query_one(Label).update(label_text)
            except Exception:
                pass

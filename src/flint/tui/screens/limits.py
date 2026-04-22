"""Limits screen — view + edit daemon-wide runtime config."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import Button, Footer, Input, Static


FIELDS = [
    ("pool_target_size", "Pool target size (rootfs warm copies)"),
    ("default_sandbox_timeout", "Default sandbox timeout (seconds)"),
    ("health_check_interval", "Health check interval (seconds)"),
    ("error_cleanup_delay", "Error-state cleanup delay (seconds)"),
]


class LimitsScreen(Screen):
    CSS = """
    LimitsScreen { background: $background; }
    LimitsScreen #limits-header { height: 3; padding: 1 2; background: $surface; }
    LimitsScreen #limits-title { color: $text-muted; }
    LimitsScreen #limits-hint { color: $text-muted; padding-top: 0; }
    LimitsScreen #limits-body { padding: 1 2; height: 1fr; }
    LimitsScreen .field-row { height: 3; padding: 0 0 1 0; }
    LimitsScreen .field-label { width: 42; color: $text-muted; }
    LimitsScreen .field-value { width: 1fr; padding: 0 1; }
    LimitsScreen #save-row { height: auto; padding-top: 1; }
    LimitsScreen #save-btn { margin-right: 1; }
    LimitsScreen #limits-status { padding: 1 0; color: $text-muted; }
    """

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back", priority=True),
        Binding("e", "toggle_edit", "Edit", priority=True),
        Binding("ctrl+s", "save", "Save", priority=True),
    ]

    editing = reactive(False)

    def compose(self) -> ComposeResult:
        with Vertical(id="limits-header"):
            yield Static("[b]Limits[/]", id="limits-title")
            yield Static("[dim]e edit · ctrl+s save · esc back[/]", id="limits-hint")
        with Vertical(id="limits-body"):
            for key, label in FIELDS:
                with Horizontal(classes="field-row"):
                    yield Static(label, classes="field-label")
                    yield Input(value="", id=f"field-{key}", classes="field-value", disabled=True)
            with Horizontal(id="save-row"):
                yield Button("Save", id="save-btn", variant="primary", disabled=True)
                yield Static("", id="limits-status")
        yield Footer()

    def on_mount(self) -> None:
        self.run_worker(self._fetch, thread=True, exclusive=True)

    def _fetch(self) -> None:
        try:
            info = self.app.client.get_config()
            self.app.state.config = info
            self.app.call_from_thread(self._apply_values)
        except Exception as e:
            self.app.call_from_thread(
                self.app.notify, f"Failed to load config: {e}", severity="error"
            )

    def _apply_values(self) -> None:
        cfg = (self.app.state.config or {}).get("config", {})
        overrides = (self.app.state.config or {}).get("overrides", {})
        for key, _ in FIELDS:
            inp = self.query_one(f"#field-{key}", Input)
            value = overrides.get(key, cfg.get(key, ""))
            inp.value = str(value)

    def watch_editing(self, editing: bool) -> None:
        for key, _ in FIELDS:
            self.query_one(f"#field-{key}", Input).disabled = not editing
        self.query_one("#save-btn", Button).disabled = not editing
        status = self.query_one("#limits-status", Static)
        status.update("[green]editing…[/]" if editing else "")

    def action_toggle_edit(self) -> None:
        self.editing = not self.editing

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save-btn":
            self.action_save()

    def action_save(self) -> None:
        if not self.editing:
            return
        overrides: dict = {}
        for key, _ in FIELDS:
            raw = self.query_one(f"#field-{key}", Input).value.strip()
            if not raw:
                continue
            try:
                if key == "health_check_interval":
                    overrides[key] = float(raw)
                else:
                    overrides[key] = int(raw)
            except ValueError:
                self.app.notify(f"Invalid value for {key}: {raw}", severity="error")
                return
        self.run_worker(lambda: self._do_save(overrides), thread=True, exclusive=False)

    def _do_save(self, overrides: dict) -> None:
        try:
            resp = self.app.client.patch_config(overrides)
            msg = f"Saved. Restart required for: {', '.join(resp.get('requires_restart', []))}"
            self.app.call_from_thread(self.app.notify, msg)
            self.app.call_from_thread(self._set_editing_false)
        except Exception as e:
            self.app.call_from_thread(
                self.app.notify, f"Save failed: {e}", severity="error"
            )

    def _set_editing_false(self) -> None:
        self.editing = False

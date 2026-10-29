"""Templates screen — templates built by the daemon."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Footer, Static


class InspectModal(ModalScreen[None]):
    CSS = """
    InspectModal { align: center middle; }
    InspectModal > Vertical {
        padding: 1 2; width: 72; height: auto; max-height: 80%;
        background: $surface; border: thick $primary;
    }
    """

    BINDINGS = [Binding("escape", "dismiss", "Close")]

    def __init__(self, title: str, body: str) -> None:
        super().__init__()
        self._title = title
        self._body = body

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(f"[b]{self._title}[/]")
            yield Static(self._body)
            yield Button("Close", id="close")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close":
            self.dismiss(None)


class TemplatesScreen(Screen):
    CSS = """
    TemplatesScreen { background: $background; color: $text; }
    TemplatesScreen #templates-header { height: 3; padding: 1 2; background: $surface; color: $text; }
    TemplatesScreen #templates-title { color: $text; }
    TemplatesScreen #templates-hint { color: $text-muted; padding-top: 0; }
    TemplatesScreen DataTable { height: 1fr; padding: 0 2; background: $surface; color: $text; }
    TemplatesScreen DataTable > .datatable--header { background: $panel; color: $text; text-style: bold; }
    TemplatesScreen DataTable > .datatable--cursor { background: $primary 30%; color: $text; }
    """

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back", priority=True),
        Binding("l", "launch", "Launch", priority=True),
        Binding("enter", "inspect", "Inspect", priority=True),
        Binding("r", "refresh", "Refresh", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="templates-header"):
            yield Static("[b]Templates[/]", id="templates-title")
            yield Static("[dim]l launch · enter inspect · esc back[/]", id="templates-hint")
        yield DataTable(id="templates-table", zebra_stripes=False, show_cursor=True)
        yield Footer()

    def on_mount(self) -> None:
        tbl = self.query_one("#templates-table", DataTable)
        tbl.add_columns("name", "kind", "status", "description")
        self._rebuild()
        self.run_worker(self._fetch, thread=True, exclusive=True)

    def _fetch(self) -> None:
        try:
            templates = self.app.client.list_templates()
            self.app.state.templates = templates
            self.app.call_from_thread(self._rebuild)
        except Exception:
            pass

    def _rebuild(self) -> None:
        tbl = self.query_one("#templates-table", DataTable)
        tbl.clear()

        # Built templates (daemon side).
        templates = self.app.state.templates or []
        for t in templates:
            name = t.get("template_id") or t.get("name") or "-"
            # Status derived from any artifact's status.
            artifacts = t.get("artifacts") or {}
            status = "ready"
            for a in artifacts.values():
                s = a.get("status", "")
                if s != "ready":
                    status = s or status
            tbl.add_row(
                name,
                "built",
                status,
                (t.get("description") or "")[:60],
                key=f"template:{name}",
            )

    def action_refresh(self) -> None:
        self.run_worker(self._fetch, thread=True, exclusive=True)

    def _current_row_key(self) -> str | None:
        tbl = self.query_one("#templates-table", DataTable)
        if tbl.row_count == 0 or tbl.cursor_row < 0:
            return None
        row_key = tbl.coordinate_to_cell_key((tbl.cursor_row, 0)).row_key
        return str(row_key.value) if row_key and row_key.value else None

    def action_launch(self) -> None:
        key = self._current_row_key()
        if not key:
            return
        template_id = key.split(":", 1)[1]
        self.run_worker(lambda: self._launch_template(template_id), thread=True, exclusive=False)

    def _launch_template(self, template_id: str) -> None:
        self.app.call_from_thread(self.app.notify, f"Launching {template_id}…")
        try:
            vm = self.app.client.create(template_id=template_id)
            self.app.call_from_thread(
                self.app.notify, f"VM {vm.get('vm_id', '?')[:8]} started"
            )
        except Exception as e:
            self.app.call_from_thread(
                self.app.notify, f"Launch failed: {e}", severity="error"
            )

    def action_inspect(self) -> None:
        key = self._current_row_key()
        if not key:
            return
        template_id = key.split(":", 1)[1]
        t = next(
            (t for t in (self.app.state.templates or []) if (t.get("template_id") or t.get("name")) == template_id),
            None,
        )
        if t is None:
            return
        artifacts = t.get("artifacts") or {}
        body = [
            f"[dim]template_id[/]  {template_id}",
            f"[dim]name[/]         {t.get('name', '-')}",
            f"[dim]description[/]  {t.get('description', '-')}",
            "",
            "[dim]artifacts[/]",
        ]
        for backend, a in artifacts.items():
            body.append(f"  {backend}: {a.get('status', '-')} @ {a.get('template_dir', '-')}")
        self.app.push_screen(InspectModal(template_id, "\n".join(body)))

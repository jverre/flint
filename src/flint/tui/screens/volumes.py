"""Volumes screen — create/list/delete shared volumes."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Footer, Input, Label, Static

from flint.tui.events import VolumesChanged


class NewVolumeModal(ModalScreen[str | None]):
    CSS = """
    NewVolumeModal { align: center middle; }
    NewVolumeModal > Vertical {
        padding: 1 2; width: 56; height: auto;
        background: $surface; border: thick $primary;
    }
    NewVolumeModal Label { padding-top: 1; color: $text-muted; }
    NewVolumeModal Horizontal { height: auto; padding-top: 1; }
    NewVolumeModal Button { margin-left: 1; }
    """

    BINDINGS = [Binding("escape", "dismiss", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("[b]New volume[/]")
            yield Label("Name")
            yield Input(placeholder="e.g. data-scratch", id="vol-name")
            with Horizontal():
                yield Static("", id="vol-error")
                yield Button("Create", variant="primary", id="create")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#vol-name", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "create":
            self._submit()
        elif event.button.id == "cancel":
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def _submit(self) -> None:
        name = self.query_one("#vol-name", Input).value.strip()
        if not name:
            self.query_one("#vol-error", Static).update("[red]name is required[/]")
            return
        self.dismiss(name)


class VolumesScreen(Screen):
    CSS = """
    VolumesScreen { background: $background; color: $text; }
    VolumesScreen #volumes-header { height: 3; padding: 1 2; background: $surface; color: $text; }
    VolumesScreen #volumes-title { color: $text; }
    VolumesScreen #volumes-hint { color: $text-muted; padding-top: 0; }
    VolumesScreen DataTable { height: 1fr; padding: 0 2; background: $surface; color: $text; }
    VolumesScreen DataTable > .datatable--header { background: $panel; color: $text; text-style: bold; }
    VolumesScreen DataTable > .datatable--cursor { background: $primary 30%; color: $text; }
    """

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back", priority=True),
        Binding("n", "new_volume", "New", priority=True),
        Binding("d", "delete_volume", "Delete", priority=True),
        Binding("r", "refresh", "Refresh", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="volumes-header"):
            yield Static("[b]Volumes[/]", id="volumes-title")
            yield Static("[dim]n new · d delete · esc back[/]", id="volumes-hint")
        yield DataTable(id="volumes-table", zebra_stripes=False, show_cursor=True)
        yield Footer()

    def on_mount(self) -> None:
        tbl = self.query_one("#volumes-table", DataTable)
        tbl.add_columns("name", "id", "created")
        self._rebuild()
        self.run_worker(self._fetch, thread=True, exclusive=True)

    def on_volumes_changed(self, event: VolumesChanged) -> None:
        self._rebuild()

    def _fetch(self) -> None:
        try:
            vols = self.app.client.list_volumes()
            self.app.state.volumes = vols
            self.app.call_from_thread(self._rebuild)
        except Exception:
            pass

    def _rebuild(self) -> None:
        import time
        tbl = self.query_one("#volumes-table", DataTable)
        tbl.clear()
        for v in self.app.state.volumes:
            created = time.strftime("%Y-%m-%d %H:%M", time.localtime(v.get("created_at", 0)))
            tbl.add_row(
                v.get("name", "-"),
                v.get("id", "-"),
                created,
                key=v.get("id"),
            )

    def action_new_volume(self) -> None:
        self.app.push_screen(NewVolumeModal(), self._on_new_result)

    def _on_new_result(self, result) -> None:
        if not result:
            return
        self.run_worker(lambda: self._do_create(result), thread=True, exclusive=False)

    def _do_create(self, name: str) -> None:
        try:
            vol = self.app.client.create_volume(name)
            # Optimistic update; event bus will also deliver volume.created.
            self.app.state.volumes = [
                v for v in self.app.state.volumes if v.get("id") != vol["id"]
            ] + [vol]
            self.app.call_from_thread(self._rebuild)
            self.app.call_from_thread(self.app.notify, f"Created volume {name}")
        except Exception as e:
            self.app.call_from_thread(
                self.app.notify, f"Failed to create volume: {e}", severity="error"
            )

    def action_delete_volume(self) -> None:
        tbl = self.query_one("#volumes-table", DataTable)
        if tbl.row_count == 0 or tbl.cursor_row < 0:
            return
        row_key = tbl.coordinate_to_cell_key((tbl.cursor_row, 0)).row_key
        volume_id = str(row_key.value) if row_key and row_key.value else None
        if not volume_id:
            return
        self.run_worker(lambda: self._do_delete(volume_id), thread=True, exclusive=False)

    def _do_delete(self, volume_id: str) -> None:
        try:
            self.app.client.delete_volume(volume_id)
            self.app.state.volumes = [v for v in self.app.state.volumes if v.get("id") != volume_id]
            self.app.call_from_thread(self._rebuild)
            self.app.call_from_thread(self.app.notify, f"Deleted volume {volume_id}")
        except Exception as e:
            self.app.call_from_thread(
                self.app.notify, f"Delete failed: {e}", severity="error"
            )

    def action_refresh(self) -> None:
        self.run_worker(self._fetch, thread=True, exclusive=True)

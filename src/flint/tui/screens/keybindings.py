"""Keybindings modal — auto-generated from App.BINDINGS + HomeScreen.BINDINGS."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from flint.tui.palette import MUTED_HEX, TEXT_HEX


class KeybindingsScreen(ModalScreen):
    CSS = """
    KeybindingsScreen { align: center middle; }
    #keybindings-modal {
        width: 56; height: auto; max-height: 80%;
        background: $surface; border: solid $primary 18%; padding: 1 2;
    }
    #keybindings-title { height: 1; text-style: bold; margin-bottom: 1; }
    .kb-section { height: 1; color: $text-muted; margin-top: 1; }
    .kb-row { height: 1; }
    #keybindings-hint { height: 1; margin-top: 1; color: $text-muted; text-style: dim; }
    """

    BINDINGS = [("escape", "dismiss", "Close")]

    def compose(self) -> ComposeResult:
        with Vertical(id="keybindings-modal"):
            yield Static("Keybindings", id="keybindings-title")
            with VerticalScroll():
                yield Static("[b]Global[/]", classes="kb-section")
                for row in self._app_rows():
                    yield Static(row, classes="kb-row")
                yield Static("[b]Home screen[/]", classes="kb-section")
                for row in self._home_rows():
                    yield Static(row, classes="kb-row")
            yield Static("Press esc to close", id="keybindings-hint")

    def on_click(self, event) -> None:
        if self.query_one("#keybindings-modal") not in event.widget.ancestors_with_self:
            self.dismiss()

    @staticmethod
    def _format_row(key: str, desc: str) -> str:
        return f"  [{TEXT_HEX}]{key:<14}[/] [{MUTED_HEX}]{desc}[/]"

    def _app_rows(self) -> list[str]:
        rows = []
        for b in getattr(self.app, "BINDINGS", []):
            key, desc = self._normalize_binding(b)
            if desc:
                rows.append(self._format_row(key, desc))
        return rows

    def _home_rows(self) -> list[str]:
        from flint.tui.screens.home import HomeScreen
        rows = []
        for b in getattr(HomeScreen, "BINDINGS", []):
            key, desc = self._normalize_binding(b)
            if desc:
                rows.append(self._format_row(key, desc))
        return rows

    @staticmethod
    def _normalize_binding(b) -> tuple[str, str]:
        """Binding can be a tuple or a textual.binding.Binding object."""
        try:
            key = getattr(b, "key", None)
            desc = getattr(b, "description", None)
            show = getattr(b, "show", True)
            if key is not None:
                return (key if show else "", desc or "" if show else "")
        except Exception:
            pass
        if isinstance(b, tuple):
            if len(b) >= 3:
                return b[0], b[2]
            if len(b) == 2:
                return b[0], b[1]
        return "", ""

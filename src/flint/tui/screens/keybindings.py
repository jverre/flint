"""Keybindings modal screen"""
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static


BINDINGS_LIST = [
    ("ctrl+up", "Previous VM"),
    ("ctrl+down", "Next VM"),
    ("+", "New VM"),
    ("ctrl+d", "Delete VM"),
    ("ctrl+b", "Benchmark"),
    ("ctrl+k", "Keybindings"),
]


class KeybindingsScreen(ModalScreen):
    CSS = """
    KeybindingsScreen {
        align: center middle;
    }

    #keybindings-modal {
        width: 40;
        height: auto;
        max-height: 80%;
        background: #1a1a1a;
        border: solid #333333;
        padding: 1 2;
    }

    #keybindings-title {
        height: 1;
        text-style: bold;
        margin-bottom: 1;
    }

    .keybinding-row {
        height: 1;
    }

    .keybinding-key {
        width: auto;
        color: white;
    }

    .keybinding-desc {
        color: $text-muted;
    }

    #keybindings-hint {
        height: 1;
        margin-top: 1;
        color: $text-muted;
        text-style: dim;
    }
    """

    BINDINGS = [("escape", "dismiss", "Close")]

    def compose(self) -> ComposeResult:
        with Vertical(id="keybindings-modal"):
            yield Static("Keybindings", id="keybindings-title")
            for key, desc in BINDINGS_LIST:
                yield Static(f"  [white]{key:<12}[/] [dim]{desc}[/]", classes="keybinding-row")
            yield Static("Press esc to close", id="keybindings-hint")

    def on_click(self, event) -> None:
        if self.query_one("#keybindings-modal") not in event.widget.ancestors_with_self:
            self.dismiss()

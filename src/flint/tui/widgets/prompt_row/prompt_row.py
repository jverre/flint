"""Prompt row widget for VM command input"""
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Input, Static


class PromptRow(Horizontal):
    """Inline prompt for sending commands to a running VM"""

    def compose(self) -> ComposeResult:
        yield Static("~ # ", classes="prompt-label")
        yield Input(id="vm-input")

    def set_prompt(self, text: str) -> None:
        """Update the prompt label to reflect the current shell prompt."""
        self.query_one(".prompt-label", Static).update(text)

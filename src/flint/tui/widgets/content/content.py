"""Content area widget for VM log display"""
from textual.app import ComposeResult
from textual.containers import Vertical
from ..terminal import Terminal


class ContentArea(Vertical):
    """Displays logs for the selected VM"""

    DEFAULT_CSS = """
    ContentArea {
        width: 1fr;
    }
    """

    def compose(self) -> ComposeResult:
        yield Terminal()

    def show_vm_logs(self, vm_id: str) -> None:
        self.query_one(Terminal).show_vm(vm_id)

    def clear_terminal(self) -> None:
        self.query_one(Terminal).clear()

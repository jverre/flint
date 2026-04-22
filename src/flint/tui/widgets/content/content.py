"""Content area: tabbed detail view for the selected VM."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import TabbedContent, TabPane

from ..terminal import Terminal
from .panes import OverviewPane, LogsPane, MetricsPane, ExecHistoryPane


class ContentArea(Vertical):
    DEFAULT_CSS = """
    ContentArea { width: 1fr; height: 1fr; background: $background; }
    ContentArea TabbedContent { height: 1fr; }
    """

    _current_vm_id: str | None = None

    def compose(self) -> ComposeResult:
        with TabbedContent(id="detail-tabs"):
            with TabPane("Overview", id="tab-overview"):
                yield OverviewPane(id="pane-overview")
            with TabPane("Terminal", id="tab-terminal"):
                yield Terminal(id="pane-terminal")
            with TabPane("Logs", id="tab-logs"):
                yield LogsPane(id="pane-logs")
            with TabPane("Metrics", id="tab-metrics"):
                yield MetricsPane(id="pane-metrics")
            with TabPane("Exec", id="tab-exec"):
                yield ExecHistoryPane(id="pane-exec")

    def show_vm_logs(self, vm_id: str) -> None:
        """Kept for back-compat with HomeScreen — delegates to show_vm."""
        self.show_vm(vm_id)

    def show_vm(self, vm_id: str) -> None:
        self._current_vm_id = vm_id
        for pane_id in ("pane-overview", "pane-terminal", "pane-logs", "pane-metrics", "pane-exec"):
            try:
                pane = self.query_one(f"#{pane_id}")
                if hasattr(pane, "show_vm"):
                    pane.show_vm(vm_id)
                elif hasattr(pane, "show_vm_logs"):
                    pane.show_vm_logs(vm_id)
            except Exception:
                pass

    def evict_vm(self, vm_id: str) -> None:
        if self._current_vm_id == vm_id:
            self._current_vm_id = None
        for pane_id in ("pane-overview", "pane-terminal", "pane-logs", "pane-metrics", "pane-exec"):
            try:
                pane = self.query_one(f"#{pane_id}")
                if hasattr(pane, "evict_vm"):
                    pane.evict_vm(vm_id)
            except Exception:
                pass

    def clear_terminal(self) -> None:
        try:
            self.query_one("#pane-terminal", Terminal).clear()
        except Exception:
            pass

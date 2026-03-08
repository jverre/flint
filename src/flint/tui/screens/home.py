from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, Input, ListView

from flint.core.config import log
from flint.core.manager import SandboxManager
from flint.tui.widgets.sidebar import Sidebar
from flint.tui.widgets.content import ContentArea
from flint.tui.widgets.terminal import Terminal
from flint.tui.widgets.prompt_row import PromptRow


class HomeScreen(Screen):
    CSS_PATH = "home.tcss"

    BINDINGS = [
        Binding("tab", "toggle_panel", "Toggle Panel", priority=True),
        Binding("s", "start_vm", "Start VM"),
        Binding("backspace", "delete_vm", "Delete VM"),
        Binding("delete", "delete_vm", "Delete VM"),
        Binding("b", "benchmark", "Benchmark"),
    ]

    _pending_auto_focus: bool = False

    @property
    def manager(self) -> SandboxManager:
        return self.app.manager

    def compose(self) -> ComposeResult:
        yield Sidebar()
        yield ContentArea()
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#vm-list", ListView).focus()

    def _sidebar_has_focus(self) -> bool:
        focused = self.app.focused
        if focused is None:
            return False
        sidebar = self.query_one(Sidebar)
        return focused is sidebar or sidebar in focused.ancestors

    def action_toggle_panel(self) -> None:
        if self._sidebar_has_focus():
            # Sidebar -> Content: focus Input if prompt is visible
            terminal = self.query_one(Terminal)
            if terminal.query_one(PromptRow).display:
                terminal.query_one("#vm-input", Input).focus()
        else:
            # Content -> Sidebar
            self.query_one("#vm-list", ListView).focus()

    def action_start_vm(self) -> None:
        if not self._sidebar_has_focus():
            return
        self.run_worker(self._start_vm_in_thread, thread=True)

    def _start_vm_in_thread(self) -> None:
        try:
            self.manager.create()
            self.app.call_from_thread(self._on_vm_started)
        except Exception:
            log.exception("Failed to start VM")

    def _on_vm_started(self) -> None:
        sidebar = self.query_one(Sidebar)
        sidebar.vm_count = len(self.manager.list())
        vm_list = self.query_one("#vm-list", ListView)
        vm_list.index = len(vm_list) - 1
        self._pending_auto_focus = True

    def action_delete_vm(self) -> None:
        if not self._sidebar_has_focus():
            return
        vm_list = self.query_one("#vm-list", ListView)
        if vm_list.highlighted_child is None:
            return
        vm_id = vm_list.highlighted_child.vm_id
        self.manager.kill(vm_id)
        content = self.query_one(ContentArea)
        content.clear_terminal()
        sidebar = self.query_one(Sidebar)
        sidebar.vm_count = len(self.manager.list())

    def on_terminal_prompt_ready(self, event: Terminal.PromptReady) -> None:
        if self._pending_auto_focus:
            self._pending_auto_focus = False
            event.input_widget.focus()

    def action_benchmark(self) -> None:
        from .benchmark import BenchmarkScreen
        self.app.push_screen(BenchmarkScreen())

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.item is not None:
            vm_id = event.item.vm_id
            content = self.query_one(ContentArea)
            content.show_vm_logs(vm_id)

import logging

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Button, Input, ListView, Static

from flint.sandbox import Sandbox
from flint.tui.widgets.sidebar import Sidebar
from flint.tui.widgets.content import ContentArea
from flint.tui.widgets.terminal import Terminal
from flint.tui.widgets.prompt_row import PromptRow

log = logging.getLogger(__name__)


class HomeScreen(Screen):
    CSS_PATH = "home.tcss"

    BINDINGS = [
        Binding("tab", "toggle_panel", "Toggle Panel", priority=True),
        Binding("backspace", "delete_vm", "Delete VM"),
        Binding("delete", "delete_vm", "Delete VM"),
        Binding("b", "benchmark", "Benchmark"),
    ]

    _pending_auto_focus: bool = False

    def compose(self) -> ComposeResult:
        yield Static("Flint", id="brand-header")
        with Horizontal(id="section-header"):
            yield Static("Virtual Machines (0)", id="section-title")
            yield Button("+", id="new-vm-btn", variant="default")
        yield Sidebar()
        yield ContentArea()

    def on_mount(self) -> None:
        self.query_one("#vm-list", ListView).focus()
        self.query_one(ContentArea).display = False

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "new-vm-btn":
            self.run_worker(self._start_vm_in_thread, thread=True)

    def _sidebar_has_focus(self) -> bool:
        focused = self.app.focused
        if focused is None:
            return False
        sidebar = self.query_one(Sidebar)
        return focused is sidebar or sidebar in focused.ancestors

    def action_toggle_panel(self) -> None:
        content = self.query_one(ContentArea)
        if not content.display:
            if self._sidebar_has_focus():
                return
        if self._sidebar_has_focus():
            terminal = self.query_one(Terminal)
            if terminal.query_one(PromptRow).display:
                terminal.query_one("#vm-input", Input).focus()
        else:
            self.query_one("#vm-list", ListView).focus()

    def _start_vm_in_thread(self) -> None:
        try:
            sandbox = Sandbox()
            self.app.sandboxes[sandbox.id] = sandbox
            self.app.call_from_thread(self._on_vm_started)
        except Exception:
            log.exception("Failed to start VM")

    def _on_vm_started(self) -> None:
        sidebar = self.query_one(Sidebar)
        vms = Sandbox.list()
        sidebar.vm_count = len(vms)
        self._update_section_title(len(vms))
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
        sandbox = self.app.sandboxes.pop(vm_id, None) or Sandbox.connect(vm_id)
        sandbox.kill()
        content = self.query_one(ContentArea)
        content.evict_vm(vm_id)
        sidebar = self.query_one(Sidebar)
        vms = Sandbox.list()
        sidebar.vm_count = len(vms)
        self._update_section_title(len(vms))
        if not vms:
            content.display = False

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
            content.display = True
            content.show_vm_logs(vm_id)

    def _update_section_title(self, count: int) -> None:
        self.query_one("#section-title", Static).update(
            f"Virtual Machines ({count})"
        )

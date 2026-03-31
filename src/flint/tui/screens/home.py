import logging

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Button, ListView, Static

from flint.sandbox import Sandbox
from flint.tui.widgets.sidebar import Sidebar
from flint.tui.widgets.content import ContentArea
from flint.tui.widgets.terminal import Terminal

log = logging.getLogger(__name__)


class HomeScreen(Screen):
    CSS_PATH = "home.tcss"

    BINDINGS = [
        Binding("ctrl+up", "prev_vm", "Previous VM", priority=True),
        Binding("ctrl+down", "next_vm", "Next VM", priority=True),
        Binding("ctrl+d", "delete_vm", "Delete VM", priority=True),
        Binding("ctrl+b", "benchmark", "Benchmark", priority=True),
        Binding("ctrl+k", "show_keybindings", "Keybindings", priority=True),
    ]

    _pending_auto_focus: bool = False

    def compose(self) -> ComposeResult:
        with Horizontal(id="main-layout"):
            yield Sidebar()
            yield ContentArea()

    def on_mount(self) -> None:
        vm_list = self.query_one("#vm-list", ListView)
        vm_list.focus()
        # If VMs already exist, select the first one; otherwise hide content
        vms = Sandbox.list()
        if vms:
            self.query_one(Sidebar).vm_count = len(vms)
            self._update_section_title(len(vms))
            self.set_timer(0.6, self._select_first_vm)
        else:
            self.query_one(ContentArea).display = False

    def _select_first_vm(self) -> None:
        vm_list = self.query_one("#vm-list", ListView)
        if vm_list.children:
            vm_list.index = 0

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "new-vm-btn":
            self.run_worker(self._start_vm_in_thread, thread=True)

    def _move_vm_selection(self, delta: int) -> None:
        vm_list = self.query_one("#vm-list", ListView)
        if not vm_list.children:
            return
        current = vm_list.index or 0
        new_index = max(0, min(len(vm_list.children) - 1, current + delta))
        vm_list.index = new_index

    def action_prev_vm(self) -> None:
        self._move_vm_selection(-1)

    def action_next_vm(self) -> None:
        self._move_vm_selection(1)

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

    def action_show_keybindings(self) -> None:
        from .keybindings import KeybindingsScreen
        self.app.push_screen(KeybindingsScreen())

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

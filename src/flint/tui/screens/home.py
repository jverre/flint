import logging

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Button, ListView, Static

from flint.sandbox import Sandbox
from flint.tui.events import SandboxesChanged
from flint.tui.widgets.sidebar import Sidebar
from flint.tui.widgets.content import ContentArea
from flint.tui.widgets.terminal import Terminal

log = logging.getLogger(__name__)


class HomeScreen(Screen):
    CSS_PATH = "home.tcss"

    BINDINGS = [
        Binding("ctrl+up", "prev_vm", "Previous VM", priority=True),
        Binding("ctrl+down", "next_vm", "Next VM", priority=True),
        Binding("ctrl+d", "delete_vm", "Delete VM(s)", priority=True),
        Binding("space", "toggle_selection", "Select", priority=True, show=False),
    ]

    _pending_auto_focus: bool = False

    def compose(self) -> ComposeResult:
        with Horizontal(id="main-layout"):
            yield Sidebar()
            yield ContentArea()

    def on_mount(self) -> None:
        vm_list = self.query_one("#vm-list", ListView)
        vm_list.focus()
        # Wait briefly for the app-level startup fetch to populate state.
        self.set_timer(0.6, self._reconcile_initial)

    def _reconcile_initial(self) -> None:
        state = getattr(self.app, "state", None)
        content = self.query_one(ContentArea)
        if state and state.sandboxes:
            self._update_section_title(len(state.sandboxes))
            vm_list = self.query_one("#vm-list", ListView)
            if vm_list.children:
                vm_list.index = 0
            content.display = True
        else:
            content.display = False

    def on_sandboxes_changed(self, event: SandboxesChanged) -> None:
        state = getattr(self.app, "state", None)
        if state is None:
            return
        count = len(state.sandboxes)
        self._update_section_title(count)
        content = self.query_one(ContentArea)
        content.display = count > 0

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

    def action_toggle_selection(self) -> None:
        vm_list = self.query_one("#vm-list", ListView)
        if vm_list.highlighted_child is None:
            return
        vm_id = getattr(vm_list.highlighted_child, "vm_id", None)
        if not vm_id:
            return
        state = self.app.state
        if vm_id in state.selected_ids:
            state.selected_ids.discard(vm_id)
        else:
            state.selected_ids.add(vm_id)
        self.query_one(Sidebar)._rebuild()

    def _start_vm_in_thread(self) -> None:
        try:
            sandbox = Sandbox()
            self.app.sandboxes[sandbox.id] = sandbox
            self.app.call_from_thread(self._on_vm_started)
        except Exception:
            log.exception("Failed to start VM")

    def _on_vm_started(self) -> None:
        vm_list = self.query_one("#vm-list", ListView)
        if vm_list.children:
            vm_list.index = len(vm_list) - 1
        self._pending_auto_focus = True

    def action_delete_vm(self) -> None:
        state = self.app.state
        targets: list[str] = []
        if state.selected_ids:
            targets = list(state.selected_ids)
        else:
            vm_list = self.query_one("#vm-list", ListView)
            if vm_list.highlighted_child is not None:
                vm_id = getattr(vm_list.highlighted_child, "vm_id", None)
                if vm_id:
                    targets = [vm_id]
        if not targets:
            return
        self.run_worker(lambda: self._bulk_kill(targets), thread=True)

    def _bulk_kill(self, vm_ids: list[str]) -> None:
        for vm_id in vm_ids:
            try:
                sandbox = self.app.sandboxes.pop(vm_id, None) or Sandbox.connect(vm_id)
                sandbox.kill()
            except Exception:
                log.exception("Failed to kill %s", vm_id)
            self.app.call_from_thread(self._on_vm_killed, vm_id)

    def _on_vm_killed(self, vm_id: str) -> None:
        self.app.state.selected_ids.discard(vm_id)
        content = self.query_one(ContentArea)
        try:
            content.evict_vm(vm_id)
        except Exception:
            pass

    def on_terminal_prompt_ready(self, event: Terminal.PromptReady) -> None:
        if self._pending_auto_focus:
            self._pending_auto_focus = False
            event.input_widget.focus()

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.item is not None:
            vm_id = event.item.vm_id
            self.app.state.selected_vm_id = vm_id
            content = self.query_one(ContentArea)
            content.display = True
            content.show_vm_logs(vm_id)

    def _update_section_title(self, count: int) -> None:
        try:
            self.query_one("#section-title", Static).update(
                f"Virtual Machines ({count})"
            )
        except Exception:
            pass

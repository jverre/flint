"""Per-VM exec history pane."""

from __future__ import annotations

import time

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Input, Static


class ExecHistoryPane(Vertical):
    DEFAULT_CSS = """
    ExecHistoryPane { height: 1fr; padding: 0 1; background: $background; }
    ExecHistoryPane #exec-toolbar { height: 1; padding: 0 1; background: $surface; }
    ExecHistoryPane #exec-input { width: 1fr; border: none; background: transparent; }
    ExecHistoryPane #exec-hint { width: auto; padding: 0 1; color: $text-muted; }
    ExecHistoryPane #exec-output { height: auto; padding: 0 1 1 1; color: $text-muted; }
    ExecHistoryPane DataTable { height: 1fr; }
    """

    BINDINGS = [
        Binding("enter", "rerun", "Re-run", show=False),
    ]

    _current_vm_id: str | None = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="exec-toolbar"):
            yield Input(placeholder="run a command…", id="exec-input")
            yield Static("enter to run", id="exec-hint")
        yield DataTable(id="exec-table", zebra_stripes=False, show_cursor=True)
        yield Static("", id="exec-output")

    def on_mount(self) -> None:
        tbl = self.query_one("#exec-table", DataTable)
        tbl.add_columns("when", "cmd", "exit")
        self._refresh()

    def show_vm(self, vm_id: str) -> None:
        self._current_vm_id = vm_id
        self._refresh()

    def evict_vm(self, vm_id: str) -> None:
        if self._current_vm_id == vm_id:
            self._current_vm_id = None
            self._refresh()
            self.query_one("#exec-output", Static).update("")

    def _refresh(self) -> None:
        tbl = self.query_one("#exec-table", DataTable)
        tbl.clear()
        vm_id = self._current_vm_id
        if not vm_id:
            return
        history = getattr(self.app, "_exec_history", {}).get(vm_id, [])
        for entry in history[-50:]:
            when = time.strftime("%H:%M:%S", time.localtime(entry["ts"]))
            cmd = entry["cmd"]
            exit_code = entry["exit_code"]
            tbl.add_row(when, cmd if len(cmd) < 60 else cmd[:57] + "…", str(exit_code))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "exec-input":
            return
        cmd = event.value.strip()
        if not cmd or not self._current_vm_id:
            return
        event.input.value = ""
        self.app.run_worker(lambda: self._run(cmd), thread=True, exclusive=False)

    def _run(self, cmd: str) -> None:
        vm_id = self._current_vm_id
        if not vm_id:
            return
        try:
            result = self.app.client.exec_command(vm_id, cmd, timeout=30)
        except Exception as e:
            result = {"stdout": "", "stderr": str(e), "exit_code": -1}
        self.app.call_from_thread(self._on_result, vm_id, cmd, result)

    def _on_result(self, vm_id: str, cmd: str, result: dict) -> None:
        history = getattr(self.app, "_exec_history", None)
        if history is None:
            history = {}
            self.app._exec_history = history
        history.setdefault(vm_id, []).append(
            {
                "ts": time.time(),
                "cmd": cmd,
                "exit_code": result.get("exit_code", -1),
                "stdout": result.get("stdout", ""),
                "stderr": result.get("stderr", ""),
            }
        )
        if self._current_vm_id == vm_id:
            self._refresh()
            out = result.get("stdout", "") or result.get("stderr", "")
            self.query_one("#exec-output", Static).update(
                f"[dim]exit={result.get('exit_code', '-')}[/]\n{out[:1000]}"
            )

    def action_rerun(self) -> None:
        tbl = self.query_one("#exec-table", DataTable)
        if tbl.row_count == 0 or self._current_vm_id is None:
            return
        row = tbl.cursor_row
        history = getattr(self.app, "_exec_history", {}).get(self._current_vm_id, [])
        if 0 <= row < len(history):
            cmd = history[row]["cmd"]
            self.app.run_worker(lambda: self._run(cmd), thread=True, exclusive=False)

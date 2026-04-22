"""Playground screen — launch an ephemeral VM and run a script in it."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, Footer, Input, RichLog, Select, Static, TextArea


RUNTIMES = [("shell", "shell"), ("python3", "python3"), ("node", "node")]


class PlaygroundScreen(Screen):
    CSS = """
    PlaygroundScreen { background: $background; }
    PlaygroundScreen #pg-header { height: 3; padding: 1 2; background: $surface; }
    PlaygroundScreen #pg-title { color: $text-muted; }
    PlaygroundScreen #pg-hint { color: $text-muted; padding-top: 0; }
    PlaygroundScreen #pg-toolbar { height: 3; padding: 1 2; background: $surface; }
    PlaygroundScreen #pg-toolbar > * { margin-right: 1; }
    PlaygroundScreen #pg-template { width: 24; }
    PlaygroundScreen #pg-runtime { width: 18; }
    PlaygroundScreen #pg-launch { min-width: 10; }
    PlaygroundScreen #pg-status { color: $text-muted; padding: 0 1; }
    PlaygroundScreen #pg-body { height: 1fr; padding: 0 2; }
    PlaygroundScreen TextArea { height: 50%; }
    PlaygroundScreen #pg-output { height: 50%; background: $surface; padding: 0 1; }
    """

    BINDINGS = [
        Binding("escape", "leave", "Back", priority=True),
        Binding("ctrl+r", "run", "Run", priority=True),
    ]

    _vm_id: str | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="pg-header"):
            yield Static("[b]Playground[/]", id="pg-title")
            yield Static("[dim]ctrl+r run · esc back (kills ephemeral VM)[/]", id="pg-hint")
        with Horizontal(id="pg-toolbar"):
            yield Input(placeholder="template (default)", id="pg-template")
            yield Select(options=RUNTIMES, value="python3", id="pg-runtime", allow_blank=False)
            yield Button("Launch & Run", variant="primary", id="pg-launch")
            yield Static("", id="pg-status")
        with Vertical(id="pg-body"):
            yield TextArea.code_editor(
                'print("hello from playground")\n',
                language="python",
                id="pg-script",
            )
            yield RichLog(id="pg-output", wrap=True, highlight=False, markup=False)
        yield Footer()

    def on_unmount(self) -> None:
        self._kill_ephemeral()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "pg-launch":
            self.action_run()

    def action_run(self) -> None:
        template_id = self.query_one("#pg-template", Input).value.strip() or "default"
        runtime = self.query_one("#pg-runtime", Select).value
        code = self.query_one("#pg-script", TextArea).text
        if not code.strip():
            self.app.notify("Script is empty", severity="warning")
            return
        self.query_one("#pg-output", RichLog).clear()
        self._set_status("[yellow]launching…[/]")
        self.run_worker(
            lambda: self._launch_and_run(template_id, runtime, code),
            thread=True,
            exclusive=True,
        )

    def action_leave(self) -> None:
        self._kill_ephemeral()
        self.app.pop_screen()

    def _set_status(self, markup: str) -> None:
        try:
            self.query_one("#pg-status", Static).update(markup)
        except Exception:
            pass

    def _launch_and_run(self, template_id: str, runtime: str, code: str) -> None:
        client = self.app.client
        try:
            if self._vm_id is None:
                vm = client.create(template_id=template_id)
                self._vm_id = vm.get("vm_id")
                self.app.call_from_thread(
                    self._set_status, f"[green]VM {self._vm_id[:8]} ready[/]"
                )
        except Exception as e:
            self.app.call_from_thread(
                self._set_status, f"[red]launch failed: {e}[/]"
            )
            return

        try:
            if runtime == "shell":
                result = client.exec_command(self._vm_id, code, timeout=60)
            else:
                result = client.run_code(self._vm_id, code, runtime=runtime, timeout=60)
        except Exception as e:
            self.app.call_from_thread(self._set_status, f"[red]exec failed: {e}[/]")
            return

        stdout = result.get("stdout", "")
        stderr = result.get("stderr", "")
        exit_code = result.get("exit_code", "?")
        lines = []
        if stdout:
            lines.append(stdout.rstrip())
        if stderr:
            lines.append("--- stderr ---")
            lines.append(stderr.rstrip())
        lines.append(f"[exit {exit_code}]")
        self.app.call_from_thread(self._append_output, "\n".join(lines))
        self.app.call_from_thread(self._set_status, f"[green]done (exit {exit_code})[/]")

    def _append_output(self, text: str) -> None:
        log = self.query_one("#pg-output", RichLog)
        log.write(text)

    def _kill_ephemeral(self) -> None:
        vm_id = self._vm_id
        if vm_id is None:
            return
        self._vm_id = None

        def _kill():
            try:
                self.app.client.kill(vm_id)
            except Exception:
                pass

        self.app.run_worker(_kill, thread=True, exclusive=False)

"""Virtual terminal widget for VM interaction"""
import re
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.events import Click
from textual.message import Message
from textual.widgets import Input, RichLog, Static
from ..prompt_row import PromptRow
from ..throbber import Throbber
from flint.core.manager import SandboxManager
from flint.core.config import TERM_COLS, TERM_ROWS
from flint.tui.terminal_emulator import TerminalEmulator

PROMPT_PATTERN = re.compile(r'[#$]\s*$')
PROMPT_PREFIX = re.compile(r'^(.*?[#$]\s*)')


class Terminal(Vertical):
    """Terminal widget using pyte for VT100 emulation via TerminalEmulator"""

    class PromptReady(Message):
        """Posted when a shell prompt becomes visible and the terminal is idle."""

        def __init__(self, input_widget: Input) -> None:
            super().__init__()
            self.input_widget = input_widget

    DEFAULT_CSS = """
    Terminal {
        height: 1fr;
    }

    #activity-bar {
        width: 100%;
        height: 1;
        visibility: hidden;
    }

    Terminal.-state-busy #activity-bar {
        visibility: visible;
    }

    #status-bar {
        height: 1;
        background: $surface;
        color: $text-muted;
        padding: 0 1;
        text-style: dim;
    }

    #log-scroll {
        height: 1fr;
        overflow-x: hidden;
    }

    #vm-log {
        height: auto;
        overflow: hidden hidden;
        padding: 0 1;
    }

    #prompt-row {
        height: 1;
        padding: 0 1;
    }

    .prompt-label {
        width: auto;
        height: 1;
        color: $primary;
        text-style: bold;
    }

    #vm-input {
        border: none;
        margin: 0;
        padding: 0;
        height: 1;
    }
    """

    _current_vm_id: str | None = None
    _lines_written: int = 0
    _last_screen_version: int = 0
    _busy: bool = False
    _current_prompt: str = "~ # "
    _emulator: TerminalEmulator | None = None
    _pty = None  # PtySession

    @property
    def manager(self) -> SandboxManager:
        return self.app.manager

    def compose(self) -> ComposeResult:
        yield Throbber(id="activity-bar")
        yield Static("", id="status-bar")
        with VerticalScroll(id="log-scroll"):
            yield RichLog(id="vm-log", wrap=True)
            yield PromptRow(id="prompt-row")

    def on_mount(self) -> None:
        self.query_one(PromptRow).display = False
        self.set_interval(0.2, self._refresh)
        self._update_status()

    def on_click(self, event: Click) -> None:
        if self.query_one(PromptRow).display:
            self.query_one("#vm-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if not self._current_vm_id or not self._pty:
            return
        command = event.value
        event.input.value = ""
        self.query_one("#vm-log", RichLog).write(f"{self._current_prompt}{command}")
        self._scroll_to_bottom()
        self._pty.send_input(f"{command}\n")
        self._set_busy()

    def _set_busy(self) -> None:
        self._busy = True
        self.remove_class("-state-idle")
        self.add_class("-state-busy")
        self.query_one(PromptRow).display = False

    def _set_idle(self, cursor_line: str = "") -> None:
        self._busy = False
        self.remove_class("-state-busy")
        self.add_class("-state-idle")
        m = PROMPT_PREFIX.match(cursor_line)
        if m:
            prefix = m.group(1)
            self._current_prompt = prefix if prefix.endswith(" ") else prefix + " "
        prompt = self.query_one(PromptRow)
        prompt.set_prompt(self._current_prompt)
        if not prompt.display:
            prompt.display = True
            self.post_message(
                self.PromptReady(self.query_one("#vm-input", Input))
            )

    def _scroll_to_bottom(self) -> None:
        self.query_one("#log-scroll", VerticalScroll).scroll_end(animate=False)

    def show_vm(self, vm_id: str) -> None:
        if self._pty:
            self._pty.kill()
            self._pty = None

        self._current_vm_id = vm_id
        self._lines_written = 0
        self._last_screen_version = 0
        self._busy = False
        self._current_prompt = "~ # "
        self.query_one(PromptRow).display = False

        # Get sandbox and create PTY session
        sandbox = self.manager.get(vm_id)
        if not sandbox:
            return
        self._emulator = TerminalEmulator(cols=TERM_COLS, rows=TERM_ROWS)
        self._pty = sandbox.pty.create(cols=TERM_COLS, rows=TERM_ROWS)
        self._pty.on_data(self._emulator.feed)

        self._render_full()
        if sandbox.tcp_connected and self._emulator:
            cursor_line = self._get_cursor_line()
            if PROMPT_PATTERN.search(cursor_line):
                self._set_idle(cursor_line)
        self._update_status()

    def clear(self) -> None:
        if self._pty:
            self._pty.kill()
            self._pty = None
        self._emulator = None
        self._current_vm_id = None
        self._lines_written = 0
        self._last_screen_version = 0
        self._busy = False
        self._current_prompt = "~ # "
        self.query_one("#vm-log", RichLog).clear()
        self.query_one(PromptRow).display = False
        self._update_status()

    def _render_log_and_screen(self, sandbox, log_widget: "RichLog") -> None:
        log_widget.clear()
        for line in list(sandbox.log_lines):
            log_widget.write(line)
        self._lines_written = sandbox.line_count
        if sandbox.tcp_connected and self._emulator:
            self._render_screen(log_widget)
            self._last_screen_version = self._emulator.version

    def _render_full(self) -> None:
        sandbox = self.manager.get(self._current_vm_id)
        if not sandbox:
            return
        self._render_log_and_screen(sandbox, self.query_one("#vm-log", RichLog))
        self._scroll_to_bottom()

    def _render_screen(self, log_widget: "RichLog") -> None:
        if not self._emulator:
            return
        screen = self._emulator.screen
        cursor_y = screen.cursor.y
        for row in range(screen.lines):
            line = screen.display[row].rstrip()
            if line:
                if row == cursor_y and PROMPT_PATTERN.search(line):
                    continue
                log_widget.write(line)

    def _get_cursor_line(self) -> str:
        if not self._emulator:
            return ""
        screen = self._emulator.screen
        return screen.display[screen.cursor.y].rstrip()

    def _refresh(self) -> None:
        if self._current_vm_id is None:
            return
        sandbox = self.manager.get(self._current_vm_id)
        if not sandbox:
            return

        boot_changed = sandbox.line_count != self._lines_written
        screen_changed = self._emulator and self._emulator.version != self._last_screen_version

        if not boot_changed and not screen_changed:
            self._update_status()
            return

        self._render_log_and_screen(sandbox, self.query_one("#vm-log", RichLog))

        if sandbox.tcp_connected and self._emulator:
            cursor_line = self._get_cursor_line()
            if PROMPT_PATTERN.search(cursor_line):
                self._set_idle(cursor_line)
            else:
                if self._busy:
                    self.query_one(PromptRow).display = False

        self._scroll_to_bottom()
        self._update_status()

    def _update_status(self) -> None:
        sandbox = self.manager.get(self._current_vm_id) if self._current_vm_id else None
        if not sandbox:
            self.query_one("#status-bar", Static).update("")
            return

        boot_str = f"{sandbox.boot_time_ms:.0f}ms" if sandbox.boot_time_ms is not None else "-"
        ready_str = f"{sandbox.ready_time_ms:.0f}ms" if sandbox.ready_time_ms is not None else "-"

        if sandbox.state == "Started" and sandbox.tcp_connected:
            icon = "[bold green]\u25cf[/]"
            status_text = "Connected"
        elif sandbox.state == "Error":
            icon = "[bold red]\u25cf[/]"
            status_text = "Error"
        elif sandbox.state == "Starting":
            icon = "[bold yellow]\u25cb[/]"
            status_text = "Booting"
        else:
            icon = "[dim]\u25cb[/]"
            status_text = sandbox.state

        short_id = self._current_vm_id[:8]
        status = (
            f" {icon} {status_text}"
            f"  [dim]boot {boot_str}  ready {ready_str}[/]"
            f"  [dim]{short_id}[/]"
        )
        self.query_one("#status-bar", Static).update(status)

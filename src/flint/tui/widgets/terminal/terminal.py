"""Virtual terminal widget for VM interaction"""
import re
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Click
from textual.message import Message
from textual.widgets import Input, RichLog, Rule, Static
from ..prompt_row import PromptRow
from flint.sandbox import Sandbox
from flint.core.config import TERM_COLS, TERM_ROWS
from flint.tui.palette import ERROR_HEX, SUCCESS_HEX, WARNING_HEX
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
        padding: 0 1 0 1;
        background: $background;
    }

    #terminal-chrome {
        width: 1fr;
        height: auto;
        padding: 1 0 0 0;
        background: $background;
    }

    #terminal-header {
        width: 1fr;
        height: 1;
        padding: 0;
        background: transparent;
    }

    #active-vm-pill {
        width: auto;
        height: 1;
        background: transparent;
    }

    #header-primary {
        width: auto;
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }

    #header-meta {
        width: auto;
        height: 1;
        padding: 0 1;
        color: $text-muted;
        background: transparent;
    }

    #header-health {
        width: auto;
        height: 1;
        padding: 0 0 0 1;
        color: $text-muted;
    }

    #header-spacer {
        width: 1fr;
        height: 1;
    }

    #terminal-separator {
        color: $border-blurred;
        margin: 0;
    }

    #log-scroll {
        height: 1fr;
        overflow-x: hidden;
        padding: 0 1 0 1;
        background: $background;
    }

    #vm-log {
        height: auto;
        overflow: hidden hidden;
        background: transparent;
        color: $text-muted;
    }

    #prompt-row {
        height: 1;
        background: $background;
    }

    .prompt-label {
        width: auto;
        height: 1;
        color: $text-muted;
    }

    #vm-input {
        border: none;
        margin: 0;
        padding: 0;
        height: 1;
        background: transparent;
        color: $text-muted;
    }
    """

    _current_vm_id: str | None = None
    _lines_written: int = 0
    _last_screen_version: int = 0
    _busy: bool = False
    _current_prompt: str = "~ # "
    _emulator: TerminalEmulator | None = None
    _vm_data: dict | None = None  # cached VM info from API
    _emulators: dict[str, TerminalEmulator]  # persistent per-VM emulators
    _sandbox: Sandbox | None = None
    _pty_sessions: dict  # vm_id -> PtySession

    def _get_sandbox(self, vm_id: str) -> Sandbox:
        """Get or create a Sandbox for the given vm_id."""
        sandboxes = self.app.sandboxes
        if vm_id not in sandboxes:
            sandboxes[vm_id] = Sandbox.connect(vm_id)
        return sandboxes[vm_id]

    def compose(self) -> ComposeResult:
        with Vertical(id="terminal-chrome"):
            with Horizontal(id="terminal-header"):
                with Horizontal(id="active-vm-pill"):
                    yield Static("", id="header-primary")
                    yield Static("", id="header-meta")
                    yield Static("", id="header-health")
                yield Static("", id="header-spacer")
            yield Rule(id="terminal-separator")
        with VerticalScroll(id="log-scroll"):
            yield RichLog(id="vm-log", wrap=True)
            yield PromptRow(id="prompt-row")

    def on_mount(self) -> None:
        self._emulators = {}
        self._pty_sessions = {}
        self.query_one(PromptRow).display = False
        self.set_interval(0.2, self._refresh)
        self._update_status()

    def on_click(self, event: Click) -> None:
        if self.query_one(PromptRow).display:
            self.query_one("#vm-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if not self._current_vm_id:
            return
        command = event.value
        event.input.value = ""
        self.query_one("#vm-log", RichLog).write(f"{self._current_prompt}{command}")
        self._scroll_to_bottom()
        pty_session = self._pty_sessions.get(self._current_vm_id)
        if pty_session:
            pty_session.send_input(f"{command}\n")
        self._set_busy()

    def _set_busy(self) -> None:
        self._busy = True
        self.query_one(PromptRow).display = False

    def _set_idle(self, cursor_line: str = "") -> None:
        self._busy = False
        m = PROMPT_PREFIX.match(cursor_line)
        if m:
            prefix = m.group(1)
            self._current_prompt = prefix if prefix.endswith(" ") else prefix + " "
        prompt = self.query_one(PromptRow)
        prompt.set_prompt(self._current_prompt)
        prompt.display = True
        inp = self.query_one("#vm-input", Input)
        inp.focus()
        self.post_message(self.PromptReady(inp))

    def _scroll_to_bottom(self) -> None:
        self.query_one("#log-scroll", VerticalScroll).scroll_end(animate=False)

    def show_vm(self, vm_id: str) -> None:
        self._current_vm_id = vm_id
        self._lines_written = 0
        self._last_screen_version = 0
        self._busy = False
        self._current_prompt = "~ # "
        self.query_one(PromptRow).display = False

        # Get sandbox and VM info
        sandbox = self._get_sandbox(vm_id)
        self._sandbox = sandbox
        vm_data = sandbox._fetch()
        if not vm_data:
            return
        self._vm_data = vm_data

        # Reuse existing emulator + PTY session, or create new ones
        if vm_id in self._emulators:
            self._emulator = self._emulators[vm_id]
        else:
            self._emulator = TerminalEmulator(cols=TERM_COLS, rows=TERM_ROWS)
            self._emulators[vm_id] = self._emulator
            pty_session = sandbox.pty.create(on_data=self._emulator.feed)
            self._pty_sessions[vm_id] = pty_session
            # New session — nudge the shell to emit a prompt
            pty_session.send_input("\n")

        self._render_full(vm_data)
        if vm_data.get("agent_healthy") and self._emulator:
            cursor_line = self._get_cursor_line()
            if PROMPT_PATTERN.search(cursor_line):
                self._set_idle(cursor_line)
        self._update_status()

    def evict_vm(self, vm_id: str) -> None:
        """Clean up emulator and PTY session for a specific VM."""
        pty_session = self._pty_sessions.pop(vm_id, None)
        if pty_session:
            pty_session.kill()
        self._emulators.pop(vm_id, None)
        if self._current_vm_id == vm_id:
            self._emulator = None
            self._sandbox = None
            self._current_vm_id = None
            self._vm_data = None
            self._lines_written = 0
            self._last_screen_version = 0
            self._busy = False
            self._current_prompt = "~ # "
            self.query_one("#vm-log", RichLog).clear()
            self.query_one(PromptRow).display = False
            self._update_status()

    def clear(self) -> None:
        if self._current_vm_id:
            self.evict_vm(self._current_vm_id)
        self._emulator = None
        self._sandbox = None
        self._current_vm_id = None
        self._vm_data = None
        self.query_one("#vm-log", RichLog).clear()
        self.query_one(PromptRow).display = False
        self._update_status()

    def _render_log_and_screen(self, vm_data: dict, log_widget: "RichLog") -> None:
        log_widget.clear()
        for line in vm_data.get("log_lines", []):
            log_widget.write(line)
        self._lines_written = vm_data.get("line_count", 0)
        if vm_data.get("agent_healthy") and self._emulator:
            self._render_screen(log_widget)
            self._last_screen_version = self._emulator.version

    def _render_full(self, vm_data: dict | None = None) -> None:
        if vm_data is None:
            if not self._sandbox:
                return
            vm_data = self._sandbox._fetch()
            if not vm_data:
                return
            self._vm_data = vm_data
        self._render_log_and_screen(vm_data, self.query_one("#vm-log", RichLog))
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
        sandbox = self._get_sandbox(self._current_vm_id)
        vm_data = sandbox._fetch()
        if not vm_data:
            return
        self._vm_data = vm_data

        boot_changed = vm_data.get("line_count", 0) != self._lines_written
        screen_changed = self._emulator and self._emulator.version != self._last_screen_version

        if not boot_changed and not screen_changed:
            self._update_status()
            return

        self._render_log_and_screen(vm_data, self.query_one("#vm-log", RichLog))

        if vm_data.get("agent_healthy") and self._emulator:
            cursor_line = self._get_cursor_line()
            if PROMPT_PATTERN.search(cursor_line):
                self._set_idle(cursor_line)
            else:
                if self._busy:
                    self.query_one(PromptRow).display = False

        self._scroll_to_bottom()
        self._update_status()

    def _update_status(self) -> None:
        pill = self.query_one("#active-vm-pill", Horizontal)
        primary_w = self.query_one("#header-primary", Static)
        meta_w = self.query_one("#header-meta", Static)
        health_w = self.query_one("#header-health", Static)
        vm_data = self._vm_data if self._current_vm_id else None
        if not vm_data:
            pill.display = False
            primary_w.update("")
            meta_w.update("")
            health_w.update("")
            return

        pill.display = True
        ready_ms = vm_data.get("ready_time_ms")
        time_str = f"{ready_ms:.0f}ms" if ready_ms is not None else "-"

        state = vm_data.get("state", "")
        agent_healthy = vm_data.get("agent_healthy", False)

        if state == "Started" and agent_healthy:
            dot = f"[{SUCCESS_HEX}]\u25cf[/]"
        elif state == "Error":
            dot = f"[{ERROR_HEX}]\u25cf[/]"
        elif state == "Starting":
            dot = f"[{WARNING_HEX}]\u25cf[/]"
        else:
            dot = "[dim]\u25cb[/]"

        short_id = self._current_vm_id[:8]
        primary_w.update(short_id)
        meta_w.update(time_str)
        health_w.update(dot)

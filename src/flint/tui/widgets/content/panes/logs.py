"""Per-VM log stream pane: RichLog + filter + follow toggle."""

from __future__ import annotations

import re

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Input, RichLog, Static, Switch

from flint._client.logs import LogStream


class LogsPane(Vertical):
    DEFAULT_CSS = """
    LogsPane { height: 1fr; padding: 0 1; background: $background; }
    LogsPane #logs-toolbar { height: 1; padding: 0 1; background: $surface; }
    LogsPane #logs-filter { width: 1fr; border: none; background: transparent; }
    LogsPane #logs-follow-label { width: auto; padding: 0 1; color: $text-muted; }
    LogsPane #logs-view { height: 1fr; padding: 0 1; background: transparent; }
    """

    _current_vm_id: str | None = None
    _stream: LogStream | None = None
    _filter_re = None
    _follow: bool = True

    def compose(self) -> ComposeResult:
        with Horizontal(id="logs-toolbar"):
            yield Input(placeholder="filter (regex)…", id="logs-filter")
            yield Static("follow", id="logs-follow-label")
            yield Switch(value=True, id="logs-follow")
        yield RichLog(id="logs-view", wrap=True, highlight=False, markup=False)

    def on_mount(self) -> None:
        pass

    def on_unmount(self) -> None:
        self._teardown_stream()

    def show_vm(self, vm_id: str) -> None:
        if self._current_vm_id == vm_id and self._stream is not None:
            return
        self._teardown_stream()
        self._current_vm_id = vm_id
        self.query_one("#logs-view", RichLog).clear()
        self._stream = LogStream(
            vm_id,
            on_line=lambda line: self.app.call_from_thread(self._on_line, line),
        )
        self._stream.start()

    def evict_vm(self, vm_id: str) -> None:
        if self._current_vm_id == vm_id:
            self._teardown_stream()
            self.query_one("#logs-view", RichLog).clear()
            self._current_vm_id = None

    def _teardown_stream(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
            except Exception:
                pass
            self._stream = None

    def _on_line(self, line: str) -> None:
        if self._filter_re and not self._filter_re.search(line):
            return
        log = self.query_one("#logs-view", RichLog)
        log.write(line)
        if self._follow:
            try:
                log.scroll_end(animate=False)
            except Exception:
                pass

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "logs-filter":
            return
        pattern = event.value or ""
        if not pattern:
            self._filter_re = None
            return
        try:
            self._filter_re = re.compile(pattern)
        except re.error:
            self._filter_re = None

    def on_switch_changed(self, event: Switch.Changed) -> None:
        if event.switch.id == "logs-follow":
            self._follow = bool(event.value)

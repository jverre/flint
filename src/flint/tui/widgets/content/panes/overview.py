"""Per-VM overview pane: static metadata + boot-timings bar chart."""

from __future__ import annotations

import time

from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static

from flint.tui.events import SandboxesChanged
from flint.tui.palette import MUTED_HEX, SUCCESS_HEX, WARNING_HEX


def _fmt_age(created_at: float) -> str:
    if not created_at:
        return "-"
    delta = time.time() - created_at
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def _bar(ms: float, max_ms: float, width: int = 24) -> str:
    if max_ms <= 0:
        return ""
    filled = max(1, int(round(ms / max_ms * width)))
    return "█" * filled


class OverviewPane(VerticalScroll):
    DEFAULT_CSS = """
    OverviewPane {
        height: 1fr; padding: 1 2; background: $surface; color: $text;
    }
    OverviewPane Static { padding: 0; background: transparent; color: $text; }
    .ov-header { color: $text-muted; padding-bottom: 1; }
    .ov-row { height: auto; }
    .ov-section { color: $text-muted; padding: 1 0 0 0; }
    """

    _current_vm_id: str | None = None

    def compose(self) -> ComposeResult:
        yield Static("Select a VM", id="ov-body")

    def on_mount(self) -> None:
        self._refresh()

    def on_sandboxes_changed(self, event: SandboxesChanged) -> None:
        self._refresh()

    def show_vm(self, vm_id: str) -> None:
        self._current_vm_id = vm_id
        self._refresh()

    def evict_vm(self, vm_id: str) -> None:
        if self._current_vm_id == vm_id:
            self._current_vm_id = None
            self._refresh()

    def _refresh(self) -> None:
        body = self.query_one("#ov-body", Static)
        vm_id = self._current_vm_id
        state = getattr(self.app, "state", None)
        vm = state.sandboxes.get(vm_id) if (state and vm_id) else None
        if not vm:
            body.update("[dim]No VM selected[/]")
            return

        short = vm_id[:12]
        ready_ms = vm.get("ready_time_ms")
        ready_str = f"{ready_ms:.0f} ms" if ready_ms else "-"
        st = vm.get("state", "?")
        dot = {
            "Started": f"[{SUCCESS_HEX}]●[/]",
            "Running": f"[{SUCCESS_HEX}]●[/]",
            "Starting": f"[{WARNING_HEX}]●[/]",
        }.get(st, "[dim]○[/]")
        agent = "healthy" if vm.get("agent_healthy") else "unhealthy"
        timings = vm.get("timings") or {}
        max_t = max(timings.values()) if timings else 0

        lines = [
            f"[b]{short}[/]  {dot} {st}",
            "",
            f"[{MUTED_HEX}]backend[/]     {vm.get('backend_kind', '-')}",
            f"[{MUTED_HEX}]template[/]    {vm.get('template_id', '-')}",
            f"[{MUTED_HEX}]pid[/]         {vm.get('pid', '-')}",
            f"[{MUTED_HEX}]agent[/]       {agent}",
            f"[{MUTED_HEX}]created[/]     {_fmt_age(vm.get('created_at', 0))}",
            f"[{MUTED_HEX}]ready_time[/]  {ready_str}",
        ]
        timeout = vm.get("timeout_seconds")
        if timeout is not None:
            lines.append(f"[{MUTED_HEX}]timeout[/]     {timeout}s ({vm.get('timeout_policy', 'kill')})")

        if timings:
            lines.append("")
            lines.append(f"[{MUTED_HEX}]Boot timings[/]")
            for step, ms in timings.items():
                lines.append(f"  {step:<22} {ms:6.1f} ms  [{SUCCESS_HEX}]{_bar(ms, max_t)}[/]")

        body.update("\n".join(lines))

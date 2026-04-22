"""Per-VM metrics pane: CPU % and RSS sparklines.

Polls `/vms/{id}/metrics` once a second. Rendering uses Unicode block glyphs
so no plotting library is required — textual-plotext is optional and used
when available for a richer chart.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static

from flint.tui.palette import SUCCESS_HEX, WARNING_HEX, MUTED_HEX


_SPARK = "▁▂▃▄▅▆▇█"


def _spark(values: list[float], max_val: float | None = None) -> str:
    if not values:
        return ""
    top = max_val if max_val is not None else max(values) or 1
    if top <= 0:
        top = 1
    out = []
    for v in values:
        ratio = max(0.0, min(1.0, v / top))
        idx = int(ratio * (len(_SPARK) - 1))
        out.append(_SPARK[idx])
    return "".join(out)


def _fmt_bytes(b: float) -> str:
    if b < 1024:
        return f"{b:.0f} B"
    for unit in ("KiB", "MiB", "GiB"):
        b /= 1024
        if b < 1024:
            return f"{b:.1f} {unit}"
    return f"{b:.1f} TiB"


class MetricsPane(VerticalScroll):
    DEFAULT_CSS = """
    MetricsPane { height: 1fr; padding: 1 2; background: $background; }
    MetricsPane Static { padding: 0; background: transparent; }
    .mx-title { color: $text-muted; padding-top: 1; }
    """

    _current_vm_id: str | None = None

    def compose(self) -> ComposeResult:
        yield Static("[dim]Select a VM[/]", id="mx-body")

    def on_mount(self) -> None:
        self.set_interval(1.0, self._tick)

    def show_vm(self, vm_id: str) -> None:
        self._current_vm_id = vm_id
        self._tick()

    def evict_vm(self, vm_id: str) -> None:
        if self._current_vm_id == vm_id:
            self._current_vm_id = None
            self.query_one("#mx-body", Static).update("[dim]No VM selected[/]")

    def _tick(self) -> None:
        vm_id = self._current_vm_id
        if not vm_id:
            return
        try:
            samples = self.app.client.get_metrics(vm_id, window=60)
        except Exception:
            return
        if not samples:
            self.query_one("#mx-body", Static).update(
                f"[dim]Waiting for metrics samples (takes ~2s after boot)…[/]"
            )
            return
        cpu = [s.get("cpu_percent", 0.0) for s in samples]
        rss = [s.get("rss_bytes", 0) for s in samples]
        last_cpu = cpu[-1] if cpu else 0.0
        last_rss = rss[-1] if rss else 0.0
        max_rss = max(rss) if rss else 0

        lines = [
            f"[{MUTED_HEX}]CPU[/]   [{SUCCESS_HEX}]{_spark(cpu, 100.0)}[/]  {last_cpu:6.1f} %",
            f"[{MUTED_HEX}]RSS[/]   [{WARNING_HEX}]{_spark(rss, max_rss)}[/]  {_fmt_bytes(last_rss)}",
            "",
            f"[dim]{len(samples)} samples · 1 Hz[/]",
        ]
        self.query_one("#mx-body", Static).update("\n".join(lines))

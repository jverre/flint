"""Benchmark screen — start N VMs sequentially and measure boot times."""
from __future__ import annotations

import random
import time
from collections import Counter

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Center, Container
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import Button, Input, Static
from textual.worker import Worker

from flint.sandbox import Sandbox
from flint.tui.palette import ACCENT_HEX, ERROR_HEX, SUCCESS_HEX, WARNING_HEX
from flint.tui.widgets.benchmark_grid import BenchmarkGrid, CellState


def _compute_stats(times: list[float]) -> dict:
    sorted_t = sorted(times)
    n = len(sorted_t)
    mid = n // 2
    median = (sorted_t[mid - 1] + sorted_t[mid]) / 2 if n % 2 == 0 else sorted_t[mid]
    return {
        "count": n,
        "min": sorted_t[0],
        "max": sorted_t[-1],
        "avg": sum(sorted_t) / n,
        "median": median,
        "p95": sorted_t[min(int(n * 0.95), n - 1)],
        "p99": sorted_t[min(int(n * 0.99), n - 1)],
    }


class BenchmarkScreen(Screen):
    CSS_PATH = "benchmark.tcss"

    BINDINGS = [
        Binding("escape", "cancel", "Close"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._grid_positions: list[int] = []
        self._vm_count: int = 0
        self._start_time: float = 0.0
        self._completed: int = 0
        self._ready_times: list[float] = []
        self._step_timings: list[dict[str, float]] = []
        self._poll_timer: Timer | None = None
        self._launch_worker: Worker | None = None
        self._phase: str = "input"
        self._last_error: str = ""
        self._use_pyroute2: bool = False
        self._use_rootfs_pool: bool = False
        self._toggle_states: dict[str, bool] = {
            "toggle-rootfs": False,
            "toggle-pyroute2": False,
        }

    def compose(self) -> ComposeResult:
        with Center(id="benchmark-input-wrapper"):
            with Container(id="benchmark-input-container"):
                yield Static("Benchmark: How many VMs?", classes="title")
                yield Input(value="16", id="benchmark-count-input")
                yield Static("\u25cb Rootfs pool", id="toggle-rootfs", classes="toggle-option")
                yield Static("\u25cb Pyroute2", id="toggle-pyroute2", classes="toggle-option")
                yield Button("Start", id="benchmark-start-button", variant="default")
        with Center(id="benchmark-grid-container"):
            yield BenchmarkGrid(id="benchmark-grid")
        yield Static("", id="benchmark-status")
        with Center(id="benchmark-results-container"):
            yield Static("", id="benchmark-results")
        yield Static("[dim]Press Escape to return[/]", id="benchmark-hint")

    def on_mount(self) -> None:
        self.query_one("#benchmark-grid-container").display = False
        self.query_one("#benchmark-status").display = False
        self.query_one("#benchmark-results-container").display = False
        self.query_one("#benchmark-count-input", Input).focus()

    def on_click(self, event) -> None:
        widget = event.widget
        if isinstance(widget, Static) and widget.id in self._toggle_states:
            self._toggle_states[widget.id] = not self._toggle_states[widget.id]
            on = self._toggle_states[widget.id]
            label = widget.id.replace("toggle-", "").replace("-", " ").title()
            dot = "\u25cf" if on else "\u25cb"
            color = SUCCESS_HEX if on else ""
            if color:
                widget.update(f"[{color}]{dot}[/] {label}")
            else:
                widget.update(f"{dot} {label}")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "benchmark-start-button" or self._phase != "input":
            return
        try:
            count = int(self.query_one("#benchmark-count-input", Input).value)
            if count < 1:
                raise ValueError
        except ValueError:
            self.notify("Enter a valid number (>= 1)", severity="error")
            return

        self._vm_count = count
        self._use_rootfs_pool = self._toggle_states.get("toggle-rootfs", False)
        self._use_pyroute2 = self._toggle_states.get("toggle-pyroute2", False)
        self._phase = "running"

        self.query_one("#benchmark-input-wrapper").display = False
        self.query_one("#benchmark-grid-container").display = True
        self.query_one("#benchmark-status").display = True

        grid = self.query_one("#benchmark-grid", BenchmarkGrid)
        grid.initialize(count)
        self._grid_positions = list(range(count))
        random.shuffle(self._grid_positions)

        self._start_time = time.monotonic()
        self._poll_timer = self.set_interval(0.1, self._update_status)
        self._launch_worker = self.run_worker(self._run_benchmark, thread=True)

    def _run_benchmark(self) -> None:
        """Sequential benchmark: create VM via daemon -> measure time -> kill -> next."""
        grid = self.query_one("#benchmark-grid", BenchmarkGrid)

        for i in range(self._vm_count):
            pos = self._grid_positions[i]
            grid.set_cell_state(pos, CellState.STARTING)

            try:
                sb = Sandbox(
                    use_pool=self._use_rootfs_pool,
                    use_pyroute2=self._use_pyroute2,
                )
                ready_ms = sb.ready_time_ms or 0.0

                self._ready_times.append(ready_ms)
                self._step_timings.append(sb.timings)
                grid.set_cell_state(pos, CellState.READY, ready_ms)
                sb.kill()
            except Exception as exc:
                self._last_error = str(exc)
                grid.set_cell_state(pos, CellState.FAILED)

            self._completed = i + 1

        self.app.call_from_thread(self._show_results)

    def _update_status(self) -> None:
        if self._phase != "running":
            return
        status = self.query_one("#benchmark-status", Static)
        elapsed = (time.monotonic() - self._start_time) * 1000

        parts = [f"Iteration: {self._completed}/{self._vm_count}"]

        if self._ready_times:
            last = self._ready_times[-1]
            avg = sum(self._ready_times) / len(self._ready_times)
            mn = min(self._ready_times)
            parts.append(f"Last: {last:,.0f} ms")
            parts.append(f"Avg: {avg:,.0f} ms")
            parts.append(f"Min: {mn:,.0f} ms")

        parts.append(f"Elapsed: {elapsed:,.0f} ms")
        status.update("    ".join(parts))

    def _show_results(self) -> None:
        self._phase = "results"
        self._update_status()
        if self._poll_timer:
            self._poll_timer.stop()

        wall_ms = (time.monotonic() - self._start_time) * 1000

        self.query_one("#benchmark-results-container").display = True
        results_widget = self.query_one("#benchmark-results", Static)

        if not self._ready_times:
            err = self._last_error or "unknown"
            results_widget.update(
                f"[bold {ERROR_HEX}]No VMs completed successfully.[/]\n\n"
                f"[dim]Last error:\n{err}[/]"
            )
            return

        s = _compute_stats(self._ready_times)

        grid = self.query_one("#benchmark-grid", BenchmarkGrid)
        state_counts = Counter(grid._cell_states)
        tti_total_ms = sum(self._ready_times)
        throughput = s["count"] / (tti_total_ms / 1000) if tti_total_ms > 0 else 0

        state_parts = []
        for st, color in [
            (CellState.READY, SUCCESS_HEX),
            (CellState.FAILED, ERROR_HEX),
            (CellState.STARTING, WARNING_HEX),
        ]:
            cnt = state_counts.get(st, 0)
            if cnt:
                state_parts.append(f"[{color}]{st.value}: {cnt}[/]")
        state_line = "  " + "   ".join(state_parts) if state_parts else ""

        step_breakdown = ""
        if self._step_timings:
            all_keys: list[str] = []
            seen: set[str] = set()
            for timings in self._step_timings:
                for key in timings:
                    if key not in seen:
                        all_keys.append(key)
                        seen.add(key)

            if all_keys:
                step_breakdown = "\n[bold]Per-step breakdown (ms):[/]\n"
                step_breakdown += f"  {'Step':<24s} {'Avg':>8s} {'Min':>8s} {'Max':>8s} {'P95':>8s}\n"
                for key in all_keys:
                    vals = [timings[key] for timings in self._step_timings if key in timings]
                    if not vals:
                        continue
                    st = _compute_stats(vals)
                    label = key.replace("_ms", "").replace("_", " ")
                    step_breakdown += (
                        f"  {label:<24s} "
                        f"[{WARNING_HEX}]{st['avg']:>7,.0f}[/] "
                        f"[{SUCCESS_HEX}]{st['min']:>7,.0f}[/] "
                        f"[{ERROR_HEX}]{st['max']:>7,.0f}[/] "
                        f"[{WARNING_HEX}]{st['p95']:>7,.0f}[/]\n"
                    )

        results_widget.update(
            f"[bold]Benchmark Complete: {self._vm_count} VMs[/]\n\n"
            f"  TTI total:    [bold]{tti_total_ms:>8,.0f} ms[/]\n"
            f"  Wall clock:   [dim]{wall_ms:>8,.0f} ms[/]  [dim](incl. teardown)[/]\n"
            f"  Throughput:   [bold]{throughput:>8.2f} VMs/s[/]\n"
            f"{state_line}\n\n"
            f"[bold]Ready time (ms):[/]\n"
            f"  Min:    [{SUCCESS_HEX}]{s['min']:>8,.0f} ms[/]"
            f"     Median: [{ACCENT_HEX}]{s['median']:>8,.0f} ms[/]\n"
            f"  Avg:    [{WARNING_HEX}]{s['avg']:>8,.0f} ms[/]"
            f"     P95:    [{WARNING_HEX}]{s['p95']:>8,.0f} ms[/]\n"
            f"  Max:    [{ERROR_HEX}]{s['max']:>8,.0f} ms[/]"
            f"     P99:    [{ERROR_HEX}]{s['p99']:>8,.0f} ms[/]\n"
            f"{step_breakdown}\n"
            f"  [dim]Press Escape to return[/]"
        )

    def action_cancel(self) -> None:
        if self._poll_timer:
            self._poll_timer.stop()
        if self._launch_worker and self._launch_worker.is_running:
            self._launch_worker.cancel()
        self.app.pop_screen()

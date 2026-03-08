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
from textual.widgets import Button, Checkbox, Footer, Input, Static
from textual.worker import Worker

from flint.core.benchmark import benchmark_vm
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

    SNAPSHOT_TIMING_KEYS = [
        "copy_rootfs_ms",
        "netns_setup_ms",
        "popen_ms",
        "wait_api_ready_ms",
        "api_snapshot_load_ms",
        "api_drives_ms",
        "api_resume_ms",
        "tcp_connect_ms",
        "exec_command_ms",
    ]

    SNAPSHOT_STEP_LABELS = {
        "copy_rootfs_ms": "Copy rootfs",
        "netns_setup_ms": "Netns + TAP",
        "popen_ms": "Popen FC",
        "wait_api_ready_ms": "Wait API ready",
        "api_snapshot_load_ms": "Snapshot load",
        "api_drives_ms": "PATCH drives",
        "api_resume_ms": "Resume VM",
        "tcp_connect_ms": "TCP connect",
        "exec_command_ms": "Exec command",
    }

    SIMPLE_SNAPSHOT_TIMING_KEYS = [
        "copy_rootfs_ms",
        "netns_create_ms",
        "tap_setup_ms",
        "popen_ms",
        "wait_api_ready_ms",
        "api_snapshot_load_ms",
        "api_drives_ms",
        "api_resume_ms",
        "tcp_connect_ms",
        "exec_command_ms",
    ]

    SIMPLE_SNAPSHOT_STEP_LABELS = {
        "copy_rootfs_ms": "Copy rootfs",
        "netns_create_ms": "Create netns",
        "tap_setup_ms": "TAP setup",
        "popen_ms": "Popen FC",
        "wait_api_ready_ms": "Wait API ready",
        "api_snapshot_load_ms": "Snapshot load",
        "api_drives_ms": "PATCH drives",
        "api_resume_ms": "Resume VM",
        "tcp_connect_ms": "TCP connect",
        "exec_command_ms": "Exec command",
    }

    def __init__(self) -> None:
        super().__init__()
        self._grid_positions: list[int] = []
        self._vm_count: int = 0
        self._start_time: float = 0.0
        self._completed: int = 0
        self._ready_times: list[float] = []
        self._timing_keys: list[str] = []
        self._step_labels: dict[str, str] = {}
        self._step_times: dict[str, list[float]] = {}
        self._poll_timer: Timer | None = None
        self._launch_worker: Worker | None = None
        self._phase: str = "input"
        self._last_error: str = ""
        self._use_pyroute2: bool = False
        self._use_rootfs_pool: bool = False

    def compose(self) -> ComposeResult:
        with Container(id="benchmark-input-container"):
            yield Static("Benchmark: How many VMs?", classes="title")
            yield Input(value="16", id="benchmark-count-input")
            yield Checkbox("Use rootfs pool", id="benchmark-rootfs-drive-checkbox", compact=True)
            yield Checkbox("Use pyroute2", id="benchmark-pyroute2-checkbox", compact=True)
            yield Button("Start", id="benchmark-start-button", variant="primary")
        with Center(id="benchmark-grid-container"):
            yield BenchmarkGrid(id="benchmark-grid")
        yield Static("", id="benchmark-status")
        with Center(id="benchmark-results-container"):
            yield Static("", id="benchmark-results")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#benchmark-grid-container").display = False
        self.query_one("#benchmark-status").display = False
        self.query_one("#benchmark-results-container").display = False
        self.query_one("#benchmark-count-input", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "benchmark-start-button" or self._phase != "input":
            return
        try:
            count = int(self.query_one("#benchmark-count-input", Input).value)
            if count < 1:
                raise ValueError
        except ValueError:
            self.notify("Enter a valid number (≥ 1)", severity="error")
            return

        self._vm_count = count
        self._use_pyroute2 = self.query_one("#benchmark-pyroute2-checkbox", Checkbox).value
        self._use_rootfs_pool = self.query_one("#benchmark-rootfs-drive-checkbox", Checkbox).value
        self._phase = "running"

        # Pick timing keys/labels based on mode
        if self._use_pyroute2:
            self._timing_keys = self.SNAPSHOT_TIMING_KEYS
            self._step_labels = self.SNAPSHOT_STEP_LABELS
        else:
            self._timing_keys = self.SIMPLE_SNAPSHOT_TIMING_KEYS
            self._step_labels = self.SIMPLE_SNAPSHOT_STEP_LABELS
        self._step_times = {k: [] for k in self._timing_keys}

        self.query_one("#benchmark-input-container").display = False
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
        """Sequential benchmark: create VM → verify TCP → tear down → next."""
        grid = self.query_one("#benchmark-grid", BenchmarkGrid)

        for i in range(self._vm_count):
            pos = self._grid_positions[i]
            grid.set_cell_state(pos, CellState.STARTING)

            result = benchmark_vm(
                use_pool=self._use_rootfs_pool,
                use_pyroute2=self._use_pyroute2,
            )

            if result["success"] and result["ready_time_ms"] is not None:
                ready_ms = result["ready_time_ms"]
                self._ready_times.append(ready_ms)
                for key in self._timing_keys:
                    val = result["timings"].get(key)
                    if val is not None:
                        self._step_times[key].append(val)
                grid.set_cell_state(pos, CellState.READY, ready_ms)
            else:
                self._last_error = result.get("error", "unknown")
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
        if self._poll_timer:
            self._poll_timer.stop()

        wall_ms = (time.monotonic() - self._start_time) * 1000

        self.query_one("#benchmark-results-container").display = True
        results_widget = self.query_one("#benchmark-results", Static)

        if not self._ready_times:
            err = self._last_error or "unknown"
            results_widget.update(
                f"[bold red]No VMs completed successfully.[/]\n\n"
                f"[dim]Last error:\n{err}[/]"
            )
            return

        s = _compute_stats(self._ready_times)

        grid = self.query_one("#benchmark-grid", BenchmarkGrid)
        state_counts = Counter(grid._cell_states)
        # Compute throughput from sum of TTI times (excludes destroy overhead)
        tti_total_ms = sum(self._ready_times)
        throughput = s['count'] / (tti_total_ms / 1000) if tti_total_ms > 0 else 0

        step_parts = []
        agg = {"avg": 0.0, "min": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0}
        for key in self._timing_keys:
            times = self._step_times[key]
            label = self._step_labels.get(key, key)
            if times:
                st = _compute_stats(times)
                for k in agg:
                    agg[k] += st[k]
                step_parts.append(
                    f"  {label:<20s}"
                    f"  avg [yellow]{st['avg']:>7,.1f}[/]"
                    f"  min [green]{st['min']:>7,.1f}[/]"
                    f"  p95 [yellow]{st['p95']:>7,.1f}[/]"
                    f"  p99 [red]{st['p99']:>7,.1f}[/]"
                    f"  max [red]{st['max']:>7,.1f}[/]"
                )
        step_parts.append(
            f"  {'─' * 20}"
            f"  {'─' * 11}"
            f"  {'─' * 11}"
            f"  {'─' * 11}"
            f"  {'─' * 11}"
            f"  {'─' * 11}\n"
            f"  {'Total':<20s}"
            f"  avg [yellow]{agg['avg']:>7,.1f}[/]"
            f"  min [green]{agg['min']:>7,.1f}[/]"
            f"  p95 [yellow]{agg['p95']:>7,.1f}[/]"
            f"  p99 [red]{agg['p99']:>7,.1f}[/]"
            f"  max [red]{agg['max']:>7,.1f}[/]"
        )
        step_lines = "\n".join(step_parts) + "\n"

        # State counts line
        state_parts = []
        for st, color in [
            (CellState.READY, "green"),
            (CellState.FAILED, "red"),
            (CellState.STARTING, "yellow"),
        ]:
            cnt = state_counts.get(st, 0)
            if cnt:
                state_parts.append(f"[{color}]{st.value}: {cnt}[/]")
        state_line = "  " + "   ".join(state_parts) if state_parts else ""

        results_widget.update(
            f"[bold]Benchmark Complete: {self._vm_count} VMs[/]\n\n"
            f"  TTI total:    [bold]{tti_total_ms:>8,.0f} ms[/]\n"
            f"  Wall clock:   [dim]{wall_ms:>8,.0f} ms[/]  [dim](incl. teardown)[/]\n"
            f"  Throughput:   [bold]{throughput:>8.2f} VMs/s[/]\n"
            f"{state_line}\n\n"
            f"[bold]Total ready time (ms):[/]\n"
            f"  Min:    [green]{s['min']:>8,.0f} ms[/]"
            f"     Median: [cyan]{s['median']:>8,.0f} ms[/]\n"
            f"  Avg:    [yellow]{s['avg']:>8,.0f} ms[/]"
            f"     P95:    [yellow]{s['p95']:>8,.0f} ms[/]\n"
            f"  Max:    [red]{s['max']:>8,.0f} ms[/]"
            f"     P99:    [red]{s['p99']:>8,.0f} ms[/]\n\n"
            f"[bold]Per-step breakdown (ms):[/]\n"
            f"{step_lines}\n"
            f"  [dim]Press Escape to return[/]"
        )

    def action_cancel(self) -> None:
        if self._poll_timer:
            self._poll_timer.stop()
        if self._launch_worker and self._launch_worker.is_running:
            self._launch_worker.cancel()
        self.app.pop_screen()

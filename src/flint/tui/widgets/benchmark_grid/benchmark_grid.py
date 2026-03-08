"""Grid of colored squares — GitHub contribution graph style.

Uses braille characters (U+2800–U+28FF) for pixel-level rendering,
giving a dense grid of colored dot-squares like GitHub's activity heatmap.

Each braille character = 2×4 dot grid. Each cell = 2 braille chars = 4×4 dots.
Terminal chars are ~1:2 aspect ratio, so 4 dots wide × 4 dots tall ≈ square.

Rendering uses render_line(y) for per-line rendering (toad Mandelbrot pattern).

Animations:
  - STARTING: pulsing braille fill patterns + warm color gradient sweep
  - Completion flash: bright flash fading to speed-based color
  - READY: full braille blocks with smooth green gradient
"""
from __future__ import annotations

import math
from enum import Enum
from time import monotonic

from rich.color import Color
from rich.segment import Segment
from rich.style import Style as RichStyle

from textual.strip import Strip
from textual.widget import Widget

# ── Braille patterns ─────────────────────────────────────────

# Full braille block (all 8 dots on)
FULL_CHAR = chr(0x28FF)  # ⣿

# Starting animation: pulse from sparse to full and back
# Braille dot layout per char:
#   dot1(1)  dot4(8)     row 0
#   dot2(2)  dot5(16)    row 1
#   dot3(4)  dot6(32)    row 2
#   dot7(64) dot8(128)   row 3
_STARTING_FRAMES = [
    chr(0x28F6),  # ⣶ rows 1-3 (6 dots)
    chr(0x28FF),  # ⣿ all rows (8 dots)
    chr(0x28F6),  # ⣶ rows 1-3 (6 dots)
    chr(0x283F),  # ⠿ rows 0-2 (6 dots)
]

# ── Layout ───────────────────────────────────────────────────

CELL_W = 2   # braille chars per cell
GAP_W = 1    # space chars between cells horizontally
MAX_ROWS = 5
# Vertical: alternating cell rows and gap rows

# ── GitHub dark theme colors ─────────────────────────────────

BG_COLOR = Color.parse("#0d1117")
FAILED_COLOR = Color.parse("#da3633")
FLASH_COLOR = Color.parse("#e8ffe8")

# Green ramp for READY cells (fast → slow)
# Brighter than GitHub's palette since braille dots are tiny
GREEN_FAST = Color.parse("#39d353")

# Warm gradient for STARTING animation (never goes too dim)
_STARTING_RAMP = [
    Color.parse("#8a7020"),
    Color.parse("#c4b030"),
    Color.parse("#f0e040"),
    Color.parse("#ffbb22"),
    Color.parse("#f0e040"),
    Color.parse("#c4b030"),
]

FLASH_DURATION = 0.35  # seconds


# ── Color math ───────────────────────────────────────────────

def _blend(c1: Color, c2: Color, t: float) -> Color:
    """Linearly interpolate between two rich Colors. t=0 → c1, t=1 → c2."""
    r1, g1, b1 = c1.triplet
    r2, g2, b2 = c2.triplet
    return Color.from_rgb(
        int(r1 + (r2 - r1) * t),
        int(g1 + (g2 - g1) * t),
        int(b1 + (b2 - b1) * t),
    )


def _gradient(colors: list[Color], pos: float) -> Color:
    """Sample a multi-stop gradient at position pos ∈ [0, 1)."""
    n = len(colors)
    scaled = pos * (n - 1)
    i = min(int(scaled), n - 2)
    frac = scaled - i
    return _blend(colors[i], colors[i + 1], frac)


# ── Data model ───────────────────────────────────────────────

class CellState(Enum):
    STARTING = "starting"
    READY = "ready"
    FAILED = "failed"


# ── Widget ───────────────────────────────────────────────────

class BenchmarkGrid(Widget):
    """Grid of colored braille squares — GitHub contribution graph style."""

    DEFAULT_CSS = """
    BenchmarkGrid {
        width: 1fr;
        height: auto;
        overflow: hidden;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._cell_states: list[CellState | None] = []
        self._cell_times: list[float | None] = []
        self._completion_times: list[float | None] = []
        self._cols: int = 0
        self._rows: int = 0

    def on_mount(self) -> None:
        self.auto_refresh = 1 / 15

    def initialize(self, count: int) -> None:
        self._cols = math.ceil(math.sqrt(count))
        self._rows = min(MAX_ROWS, math.ceil(count / self._cols))
        self._cell_states = [None] * count
        self._cell_times = [None] * count
        self._completion_times = [None] * count
        # Cell rows + gap rows between them
        total_h = max(1, 2 * self._rows - 1)
        self.styles.height = total_h
        self.refresh(layout=True)

    def set_cell_state(
        self, index: int, state: CellState, time_ms: float | None = None
    ) -> None:
        """Update cell state. Thread-safe (GIL protects list element writes).

        No explicit refresh — auto_refresh at 15fps handles visual updates.
        """
        if 0 <= index < len(self._cell_states):
            self._cell_states[index] = state
            self._cell_times[index] = time_ms
            if state == CellState.READY:
                self._completion_times[index] = monotonic()

    # ── Cell appearance ──────────────────────────────────────

    def _cell_appearance(self, idx: int) -> tuple[str, Color]:
        """Get the braille character and foreground color for a cell."""
        if idx >= len(self._cell_states) or self._cell_states[idx] is None:
            return FULL_CHAR, BG_COLOR

        state = self._cell_states[idx]

        if state == CellState.STARTING:
            t = monotonic()
            phase = (t * 1.5 + idx * 0.15) % 1.0
            # Pulsing dot pattern
            n = len(_STARTING_FRAMES)
            frame_idx = int(phase * n) % n
            char = _STARTING_FRAMES[frame_idx]
            # Warm gradient color sweep
            color = _gradient(_STARTING_RAMP, phase)
            return char, color

        if state == CellState.FAILED:
            return FULL_CHAR, FAILED_COLOR

        # READY — flash bright then settle to green
        color = GREEN_FAST
        ct = (
            self._completion_times[idx]
            if idx < len(self._completion_times)
            else None
        )
        if ct is not None:
            elapsed = monotonic() - ct
            if elapsed < FLASH_DURATION:
                color = _blend(FLASH_COLOR, color, elapsed / FLASH_DURATION)
        return FULL_CHAR, color

    # ── Line rendering ───────────────────────────────────────

    def render_line(self, y: int) -> Strip:
        """Render one terminal row of the grid.

        Even y = cell row (braille characters), odd y = gap row (background).
        """
        width = self.content_size.width
        if self._rows == 0 or width == 0:
            return Strip.blank(width)

        bg_style = RichStyle.from_color(bgcolor=BG_COLOR)

        # Odd y = gap row
        if y % 2 == 1:
            return Strip([Segment(" " * width, bg_style)], cell_length=width)

        grid_row = y // 2
        if grid_row >= self._rows:
            return Strip([Segment(" " * width, bg_style)], cell_length=width)

        # Build cell row
        segments: list[Segment] = []
        grid_w = self._cols * CELL_W + max(0, self._cols - 1) * GAP_W
        pad_left = max(0, (width - grid_w) // 2)

        if pad_left:
            segments.append(Segment(" " * pad_left, bg_style))

        for col in range(self._cols):
            if col > 0:
                segments.append(Segment(" " * GAP_W, bg_style))
            idx = grid_row * self._cols + col
            char, color = self._cell_appearance(idx)
            cell_style = RichStyle.from_color(color, BG_COLOR)
            segments.append(Segment(char * CELL_W, cell_style))

        used = pad_left + grid_w
        remaining = width - used
        if remaining > 0:
            segments.append(Segment(" " * remaining, bg_style))

        return Strip(segments, cell_length=width)

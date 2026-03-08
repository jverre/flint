"""Animated bar for busy state feedback."""
from __future__ import annotations

import math
from time import monotonic
from typing import Callable

from rich.segment import Segment
from rich.style import Style as RichStyle

from textual.css.styles import RulesMap
from textual.strip import Strip
from textual.style import Style
from textual.visual import RenderOptions, Visual
from textual.widget import Widget

# Thin block characters from dimmest to brightest
BLOCKS = [" ", "\u2591", "\u2592", "\u2593"]


class ThrobberVisual(Visual):
    """A dim pulsing bar with a subtle sliding highlight."""

    def __init__(self, get_time: Callable[[], float] = monotonic) -> None:
        self.get_time = get_time

    def render_strips(
        self, width: int, height: int | None, style: Style, options: RenderOptions
    ) -> list[Strip]:
        t = self.get_time()
        color = style.rich_style.color or RichStyle.parse("gray").color
        bg = style.rich_style.bgcolor
        rich_style = RichStyle.from_color(color, bg)

        pos = (t % 3.0) / 3.0
        center = pos * width
        spread = width * 0.15
        segments = []
        for i in range(width):
            dist = abs(i - center)
            alpha = math.exp(-(dist * dist) / (2 * spread * spread))
            if alpha > 0.5:
                char = BLOCKS[3]
            elif alpha > 0.2:
                char = BLOCKS[2]
            elif alpha > 0.05:
                char = BLOCKS[1]
            else:
                char = BLOCKS[0]
            segments.append(Segment(char, rich_style))
        return [Strip(segments, cell_length=width)]

    def get_optimal_width(self, rules: RulesMap, container_width: int) -> int:
        return container_width

    def get_height(self, rules: RulesMap, width: int) -> int:
        return 1


class Throbber(Widget):
    """Animated throbber bar."""

    def on_mount(self) -> None:
        self.auto_refresh = 1 / 15

    def render(self) -> ThrobberVisual:
        return ThrobberVisual()

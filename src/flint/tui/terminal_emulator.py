import pyte


class TerminalEmulator:
    """Wraps pyte for VT100 terminal emulation. TUI-side only."""

    def __init__(self, cols: int = 120, rows: int = 40) -> None:
        self.screen = pyte.Screen(cols, rows)
        self.stream = pyte.Stream(self.screen)
        self.version = 0

    def feed(self, data: bytes) -> None:
        self.stream.feed(data.decode(errors="replace"))
        self.version += 1

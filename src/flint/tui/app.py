from textual.app import App, ComposeResult
from textual.screen import Screen
from textual.widgets import Static, LoadingIndicator
from textual.containers import Center, Middle, Container

import shutil

from flint.core.manager import SandboxManager
from flint.core.config import GOLDEN_DIR
from flint.core._snapshot import create_golden_snapshot
from flint.core._pool import start_pool, stop_pool
from flint.tui.screens.home import HomeScreen


class LoadingScreen(Screen):
    DEFAULT_CSS = """
    LoadingScreen {
        align: center middle;
    }
    #loading-box {
        width: 50;
        height: 5;
        padding: 1 2;
    }
    #loading-label {
        text-align: center;
        width: 1fr;
    }
    #loading-indicator {
        width: 1fr;
        height: 1;
    }
    """

    def compose(self) -> ComposeResult:
        with Center():
            with Middle():
                with Container(id="loading-box"):
                    yield Static("Creating golden snapshot...", id="loading-label")
                    yield LoadingIndicator(id="loading-indicator")


class FlintApp(App):
    CSS_PATH = "app.tcss"
    SCREENS = {"home": HomeScreen}

    def __init__(self) -> None:
        super().__init__()
        self.manager = SandboxManager()

    def on_mount(self) -> None:
        shutil.rmtree(GOLDEN_DIR, ignore_errors=True)
        self.push_screen(LoadingScreen())
        self.run_worker(self._create_snapshot_and_boot, thread=True)

    def _boot(self) -> None:
        start_pool()
        self.push_screen("home")

    def _create_snapshot_and_boot(self) -> None:
        create_golden_snapshot()
        self.call_from_thread(self._on_snapshot_ready)

    def _on_snapshot_ready(self) -> None:
        self.pop_screen()
        self._boot()

    def on_unmount(self) -> None:
        stop_pool()

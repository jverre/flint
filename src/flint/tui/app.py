from textual.app import App

from flint.tui.screens.home import HomeScreen
from flint.tui.theme import flint_dark, flint_light


class FlintApp(App):
    CSS_PATH = "app.tcss"
    SCREENS = {"home": HomeScreen}

    def __init__(self) -> None:
        super().__init__()
        self.sandboxes: dict[str, "Sandbox"] = {}
        self.register_theme(flint_dark)
        self.register_theme(flint_light)
        self.theme = "flint-dark"

    def on_mount(self) -> None:
        self.push_screen("home")

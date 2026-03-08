from textual.app import App

from flint.tui.screens.home import HomeScreen


class FlintApp(App):
    CSS_PATH = "app.tcss"
    SCREENS = {"home": HomeScreen}

    def __init__(self) -> None:
        super().__init__()
        self.sandboxes: dict[str, "Sandbox"] = {}

    def on_mount(self) -> None:
        self.push_screen("home")

from __future__ import annotations

import logging
import threading

from textual.app import App
from textual.binding import Binding

from flint._client.client import DaemonClient
from flint._client.events import EventStream
from flint.tui.commands import FlintProvider
from flint.tui.events import SandboxesChanged, VolumesChanged, WsStateChanged
from flint.tui.palette import FLINT_THEME
from flint.tui.screens.home import HomeScreen
from flint.tui.screens.benchmark import BenchmarkScreen
from flint.tui.screens.keybindings import KeybindingsScreen
from flint.tui.screens.volumes import VolumesScreen
from flint.tui.screens.templates import TemplatesScreen
from flint.tui.screens.limits import LimitsScreen
from flint.tui.screens.playground import PlaygroundScreen
from flint.tui.state import AppState

log = logging.getLogger(__name__)


class FlintApp(App):
    CSS_PATH = "app.tcss"
    COMMANDS = {FlintProvider}

    SCREENS = {
        "home": HomeScreen,
        "benchmark": BenchmarkScreen,
        "keybindings": KeybindingsScreen,
        "volumes": VolumesScreen,
        "templates": TemplatesScreen,
        "limits": LimitsScreen,
        "playground": PlaygroundScreen,
    }

    BINDINGS = [
        Binding("ctrl+p", "command_palette", "Commands", priority=True),
        Binding("ctrl+v", "push_screen('volumes')", "Volumes", priority=True),
        Binding("ctrl+t", "push_screen('templates')", "Templates", priority=True),
        Binding("ctrl+l", "push_screen('limits')", "Limits", priority=True),
        Binding("ctrl+b", "push_screen('benchmark')", "Benchmark", priority=True),
        Binding("ctrl+g", "push_screen('playground')", "Playground", priority=True),
        Binding("question_mark", "push_screen('keybindings')", "Help", priority=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.register_theme(FLINT_THEME)
        self.theme = FLINT_THEME.name
        self.state = AppState()
        self.client = DaemonClient()
        # Keep the old attr name the existing home screen expects for Sandbox cache.
        self.sandboxes: dict[str, "Sandbox"] = {}
        self._event_stream: EventStream | None = None
        self._fetch_lock = threading.Lock()

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def on_mount(self) -> None:
        self.push_screen("home")
        # Kick off initial fetch + event stream in a worker thread so the UI
        # paints without waiting for network I/O.
        self.run_worker(self._startup_fetch, thread=True, exclusive=True)

    def _startup_fetch(self) -> None:
        try:
            with self._fetch_lock:
                vms = self.client.list()
                volumes = self.client.list_volumes()
                templates = self.client.list_templates()
                config_info = self.client.get_config()
            self.call_from_thread(self._apply_startup_snapshot, vms, volumes, templates, config_info)
        except Exception:
            log.exception("Startup fetch failed")
        # Begin the event stream regardless; it'll retry until the daemon is up.
        self._start_event_stream()

    def _apply_startup_snapshot(self, vms, volumes, templates, config_info) -> None:
        self.state.sandboxes = {v["vm_id"]: v for v in vms}
        self.state.volumes = volumes
        self.state.templates = templates
        self.state.config = config_info
        self._broadcast(lambda: SandboxesChanged(reason="snapshot"))
        self._broadcast(lambda: VolumesChanged(reason="snapshot"))

    def _start_event_stream(self) -> None:
        if self._event_stream is not None:
            return
        self._event_stream = EventStream(
            on_event=lambda ev: self.call_from_thread(self.apply_event, ev),
            on_resync=lambda: self.call_from_thread(self._resync),
            on_connected=lambda: self.call_from_thread(self._set_ws_connected, True),
            on_disconnected=lambda: self.call_from_thread(self._set_ws_connected, False),
        )
        self._event_stream.start()

    def _set_ws_connected(self, connected: bool) -> None:
        self.state.ws_connected = connected
        self._broadcast(lambda: WsStateChanged(connected=connected))

    def _resync(self) -> None:
        self.run_worker(self._startup_fetch, thread=True, exclusive=True)

    # ── Event handling (runs on UI thread) ─────────────────────────────────

    def apply_event(self, event: dict) -> None:
        etype = event.get("type", "")
        if etype == "vm.created":
            vm = event.get("vm") or {}
            if vm.get("vm_id"):
                self.state.sandboxes[vm["vm_id"]] = vm
                self._broadcast(lambda: SandboxesChanged(reason=etype))
        elif etype == "vm.deleted":
            vm_id = event.get("vm_id")
            if vm_id:
                self.state.sandboxes.pop(vm_id, None)
                self.state.selected_ids.discard(vm_id)
                if self.state.selected_vm_id == vm_id:
                    self.state.selected_vm_id = None
                self._broadcast(lambda: SandboxesChanged(reason=etype))
        elif etype == "vm.state_changed":
            vm_id = event.get("vm_id")
            to_state = event.get("to")
            if vm_id and vm_id in self.state.sandboxes:
                self.state.sandboxes[vm_id]["state"] = to_state or self.state.sandboxes[vm_id].get("state")
                self._broadcast(lambda: SandboxesChanged(reason=etype))
        elif etype == "vm.paused":
            vm_id = event.get("vm_id")
            if vm_id and vm_id in self.state.sandboxes:
                self.state.sandboxes[vm_id]["state"] = "Paused"
                self._broadcast(lambda: SandboxesChanged(reason=etype))
        elif etype == "vm.resumed":
            vm = event.get("vm") or {}
            if vm.get("vm_id"):
                self.state.sandboxes[vm["vm_id"]] = vm
                self._broadcast(lambda: SandboxesChanged(reason=etype))
        elif etype == "volume.created":
            vol = event.get("volume") or {}
            if vol.get("id"):
                self.state.volumes = [v for v in self.state.volumes if v.get("id") != vol["id"]] + [vol]
                self._broadcast(lambda: VolumesChanged(reason=etype))
        elif etype == "volume.deleted":
            vid = event.get("volume_id")
            if vid:
                self.state.volumes = [v for v in self.state.volumes if v.get("id") != vid]
                self._broadcast(lambda: VolumesChanged(reason=etype))

    def _broadcast(self, factory) -> None:
        """Post a fresh message to every widget in every mounted screen.

        Textual messages don't bubble down from screen to descendants, so to
        reach child widgets' ``on_<name>`` handlers we post a dedicated
        instance to each. ``factory`` is a callable returning a fresh Message.
        """
        for screen in list(self.screen_stack):
            try:
                screen.post_message(factory())
            except Exception:
                pass
            try:
                widgets = list(screen.query("*"))
            except Exception:
                widgets = []
            for widget in widgets:
                try:
                    widget.post_message(factory())
                except Exception:
                    pass

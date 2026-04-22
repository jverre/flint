"""Templates screen — merges daemon-built templates with the agents catalog."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Footer, Static

from flint.agents.catalog import list_agents


def _is_agent_row(row_key: str | None) -> bool:
    return bool(row_key) and row_key.startswith("agent:")


class InspectModal(ModalScreen[None]):
    CSS = """
    InspectModal { align: center middle; }
    InspectModal > Vertical {
        padding: 1 2; width: 72; height: auto; max-height: 80%;
        background: $surface; border: thick $primary;
    }
    """

    BINDINGS = [Binding("escape", "dismiss", "Close")]

    def __init__(self, title: str, body: str) -> None:
        super().__init__()
        self._title = title
        self._body = body

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(f"[b]{self._title}[/]")
            yield Static(self._body)
            yield Button("Close", id="close")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close":
            self.dismiss(None)


class TemplatesScreen(Screen):
    CSS = """
    TemplatesScreen { background: $background; }
    TemplatesScreen #templates-header { height: 3; padding: 1 2; background: $surface; }
    TemplatesScreen #templates-title { color: $text-muted; }
    TemplatesScreen #templates-hint { color: $text-muted; padding-top: 0; }
    TemplatesScreen DataTable { height: 1fr; padding: 0 2; }
    """

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back", priority=True),
        Binding("l", "launch", "Launch", priority=True),
        Binding("enter", "inspect", "Inspect", priority=True),
        Binding("r", "refresh", "Refresh", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="templates-header"):
            yield Static("[b]Templates[/]", id="templates-title")
            yield Static("[dim]l launch · enter inspect · esc back[/]", id="templates-hint")
        yield DataTable(id="templates-table", zebra_stripes=False, show_cursor=True)
        yield Footer()

    def on_mount(self) -> None:
        tbl = self.query_one("#templates-table", DataTable)
        tbl.add_columns("name", "kind", "status", "description")
        self._rebuild()
        self.run_worker(self._fetch, thread=True, exclusive=True)

    def _fetch(self) -> None:
        try:
            templates = self.app.client.list_templates()
            self.app.state.templates = templates
            self.app.call_from_thread(self._rebuild)
        except Exception:
            pass

    def _rebuild(self) -> None:
        tbl = self.query_one("#templates-table", DataTable)
        tbl.clear()

        # Built templates (daemon side).
        templates = self.app.state.templates or []
        built_names: set[str] = set()
        for t in templates:
            name = t.get("template_id") or t.get("name") or "-"
            built_names.add(name)
            # Status derived from any artifact's status.
            artifacts = t.get("artifacts") or {}
            status = "ready"
            for a in artifacts.values():
                s = a.get("status", "")
                if s != "ready":
                    status = s or status
            tbl.add_row(
                name,
                "built",
                status,
                (t.get("description") or "")[:60],
                key=f"template:{name}",
            )

        # Agent catalog.
        for agent in list_agents():
            tag = "agent (built)" if agent.name in built_names else "agent"
            status = "buildable" if agent.name not in built_names else "ready"
            tbl.add_row(
                agent.name,
                tag,
                status,
                agent.description[:60],
                key=f"agent:{agent.name}",
            )

    def action_refresh(self) -> None:
        self.run_worker(self._fetch, thread=True, exclusive=True)

    def _current_row_key(self) -> str | None:
        tbl = self.query_one("#templates-table", DataTable)
        if tbl.row_count == 0 or tbl.cursor_row < 0:
            return None
        row_key = tbl.coordinate_to_cell_key((tbl.cursor_row, 0)).row_key
        return str(row_key.value) if row_key and row_key.value else None

    def action_launch(self) -> None:
        key = self._current_row_key()
        if not key:
            return
        if key.startswith("template:"):
            template_id = key.split(":", 1)[1]
            self.run_worker(lambda: self._launch_template(template_id), thread=True, exclusive=False)
        elif key.startswith("agent:"):
            agent_name = key.split(":", 1)[1]
            self.run_worker(lambda: self._launch_agent(agent_name), thread=True, exclusive=False)

    def _launch_template(self, template_id: str) -> None:
        self.app.call_from_thread(self.app.notify, f"Launching {template_id}…")
        try:
            vm = self.app.client.create(template_id=template_id)
            self.app.call_from_thread(
                self.app.notify, f"VM {vm.get('vm_id', '?')[:8]} started"
            )
        except Exception as e:
            self.app.call_from_thread(
                self.app.notify, f"Launch failed: {e}", severity="error"
            )

    def _launch_agent(self, agent_name: str) -> None:
        self.app.call_from_thread(
            self.app.notify,
            f"Deploying {agent_name} (builds template if missing — may take minutes)…",
        )
        try:
            from flint.agents.agent import Agent
            agent = Agent.deploy(agent_name)
            self.app.call_from_thread(
                self.app.notify, f"Agent {agent_name} started in {agent.sandbox.id[:8]}"
            )
        except Exception as e:
            self.app.call_from_thread(
                self.app.notify, f"Deploy failed: {e}", severity="error"
            )

    def action_inspect(self) -> None:
        key = self._current_row_key()
        if not key:
            return
        if key.startswith("template:"):
            template_id = key.split(":", 1)[1]
            t = next(
                (t for t in (self.app.state.templates or []) if (t.get("template_id") or t.get("name")) == template_id),
                None,
            )
            if t is None:
                return
            artifacts = t.get("artifacts") or {}
            body = [
                f"[dim]template_id[/]  {template_id}",
                f"[dim]name[/]         {t.get('name', '-')}",
                f"[dim]description[/]  {t.get('description', '-')}",
                "",
                "[dim]artifacts[/]",
            ]
            for backend, a in artifacts.items():
                body.append(f"  {backend}: {a.get('status', '-')} @ {a.get('template_dir', '-')}")
            self.app.push_screen(InspectModal(template_id, "\n".join(body)))
        else:
            agent_name = key.split(":", 1)[1]
            agent = next((a for a in list_agents() if a.name == agent_name), None)
            if agent is None:
                return
            body = "\n".join([
                f"[dim]name[/]         {agent.name}",
                f"[dim]version[/]      {agent.version}",
                f"[dim]description[/]  {agent.description}",
                f"[dim]homepage[/]     {agent.homepage}",
                f"[dim]license[/]      {agent.license}",
                f"[dim]rootfs_size[/]  {agent.rootfs_size_mb} MiB",
                f"[dim]tags[/]         {', '.join(agent.tags)}",
                f"[dim]image[/]        {agent.docker_image}",
            ])
            self.app.push_screen(InspectModal(agent.name, body))

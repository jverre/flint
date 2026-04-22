"""Custom command-palette provider.

Registered on `FlintApp.COMMANDS`. Invoked via ``ctrl+p``.

Yields verb-based hits: open-screen commands (Volumes/Templates/Limits/
Playground/Benchmark/Keybindings) and per-VM actions (Kill, Pause, Resume)
built from the current ``AppState.sandboxes`` dict.
"""

from __future__ import annotations

from functools import partial
from typing import Callable

from textual.command import DiscoveryHit, Hit, Provider


SCREEN_VERBS: list[tuple[str, str]] = [
    ("Open Volumes", "volumes"),
    ("Open Templates", "templates"),
    ("Open Limits", "limits"),
    ("Open Playground", "playground"),
    ("Open Benchmark", "benchmark"),
    ("Open Keybindings", "keybindings"),
]


class FlintProvider(Provider):
    async def discover(self):
        for title, screen in SCREEN_VERBS:
            yield DiscoveryHit(
                display=title,
                command=partial(self.app.push_screen, screen),
            )
        yield DiscoveryHit(
            display="Create VM (default template)",
            command=self._create_vm,
        )

    async def search(self, query: str):
        matcher = self.matcher(query)
        for title, screen in SCREEN_VERBS:
            score = matcher.match(title)
            if score > 0:
                yield Hit(
                    score=score,
                    match_display=matcher.highlight(title),
                    command=partial(self.app.push_screen, screen),
                )

        create_text = "Create VM"
        score = matcher.match(create_text)
        if score > 0:
            yield Hit(
                score=score,
                match_display=matcher.highlight(create_text),
                command=self._create_vm,
            )

        # Per-VM actions.
        state = getattr(self.app, "state", None)
        if state is None:
            return
        for vm_id, vm in state.sandboxes.items():
            st = vm.get("state", "")
            short = vm_id[:12]
            for verb, action in self._verbs_for(st):
                label = f"{verb} {short}"
                score = matcher.match(label)
                if score > 0:
                    yield Hit(
                        score=score,
                        match_display=matcher.highlight(label),
                        command=partial(self._run_vm_action, action, vm_id),
                        help=f"state={st}",
                    )

    def _verbs_for(self, state: str) -> list[tuple[str, str]]:
        if state == "Paused":
            return [("Resume", "resume"), ("Kill", "kill")]
        return [("Kill", "kill"), ("Pause", "pause")]

    def _create_vm(self) -> None:
        self.app.run_worker(self._do_create_vm, thread=True, exclusive=False)

    def _do_create_vm(self) -> None:
        try:
            self.app.client.create()
        except Exception:
            pass

    def _run_vm_action(self, action: str, vm_id: str) -> None:
        self.app.run_worker(
            lambda: self._do_vm_action(action, vm_id), thread=True, exclusive=False
        )

    def _do_vm_action(self, action: str, vm_id: str) -> None:
        client = self.app.client
        try:
            if action == "kill":
                client.kill(vm_id)
            elif action == "pause":
                client.pause(vm_id)
            elif action == "resume":
                client.resume(vm_id)
        except Exception:
            pass

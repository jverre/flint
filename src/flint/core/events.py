"""Where core reports lifecycle events.

Core never imports the daemon. Whoever owns the process (the daemon) passes
an :data:`EventSink` to the :class:`~flint.core.manager.SandboxManager` and
:class:`~flint.core._state_store.StateStore`; without one, events go nowhere.
"""

from __future__ import annotations

from typing import Any, Protocol


class EventSink(Protocol):
    def __call__(self, event_type: str, /, **fields: Any) -> None: ...


def discard(event_type: str, /, **fields: Any) -> None:
    """The default sink: nobody is listening."""

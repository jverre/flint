"""Helpers for bulk-action progress feedback.

For the initial cut this is a thin wrapper over ``App.notify`` — one
notification per action + a final summary. A full progress overlay is out of
scope for tonight; this keeps the API stable so a richer widget can replace
it without touching callers.
"""

from __future__ import annotations

from typing import Callable, Iterable


def run_bulk(
    app,
    title: str,
    items: Iterable[str],
    action: Callable[[str], None],
    *,
    on_item_done: Callable[[str, bool], None] | None = None,
) -> None:
    """Execute `action(item)` for each item. Runs synchronously in the caller's
    thread — callers should invoke from a worker thread via `run_worker`.
    """
    items = list(items)
    total = len(items)
    if total == 0:
        return
    app.call_from_thread(app.notify, f"{title}: 0/{total}")
    ok = 0
    for i, item in enumerate(items, 1):
        success = True
        try:
            action(item)
        except Exception:
            success = False
        if success:
            ok += 1
        if on_item_done is not None:
            try:
                on_item_done(item, success)
            except Exception:
                pass
        app.call_from_thread(app.notify, f"{title}: {i}/{total}")
    app.call_from_thread(
        app.notify,
        f"{title} complete — {ok}/{total} succeeded",
        severity="information" if ok == total else "warning",
    )

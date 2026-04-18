"""Unit-test conftest: opts out of the session-wide daemon fixture.

Overrides the session-scoped ``_ensure_daemon`` fixture from the parent
``tests/conftest.py`` so these tests run without spinning up a real Flint
daemon. They exercise pure-Python plugin registry code and the CLI surface
via Click's ``CliRunner``, both of which are daemon-free.
"""

from __future__ import annotations

import pytest


@pytest.fixture(scope="session", autouse=True)
def _ensure_daemon():  # noqa: PT004 — intentional override
    yield


@pytest.fixture(scope="session", autouse=True)
def _sandbox_probe():  # noqa: PT004
    yield

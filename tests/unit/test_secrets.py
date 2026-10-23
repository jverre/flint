"""Credentials in network policies: sealed at rest, redacted on read, kept on read-modify-write."""

from __future__ import annotations

import pytest

from flint.core import _secrets


def _policy(**domains: dict[str, str]) -> dict:
    return {"allow": {d: [{"transform": [{"headers": h}]}] for d, h in domains.items()}}


def test_sealed_policy_holds_no_plaintext_and_opens_again():
    sealed = _secrets.seal('{"Authorization": "Bearer sk-live-123456"}')
    assert "sk-live" not in sealed
    assert _secrets.unseal(sealed) == '{"Authorization": "Bearer sk-live-123456"}'


def test_values_stored_before_sealing_still_read():
    assert _secrets.unseal('{"allow": {}}') == '{"allow": {}}'


def test_a_sealed_value_needs_its_key(monkeypatch):
    sealed = _secrets.seal("secret")
    monkeypatch.setattr(_secrets, "_fernet", None)
    monkeypatch.setenv("FLINT_SECRET_KEY", "another key")
    with pytest.raises(RuntimeError, match="decrypt"):
        _secrets.unseal(sealed)


def test_redacted_policy_shows_no_credential():
    got = _secrets.redact_policy(_policy(**{"api.example.com": {"Authorization": "Bearer sk-live-123456", "X-Pin": "1234"}}))
    headers = got["allow"]["api.example.com"][0]["transform"][0]["headers"]
    assert headers == {"Authorization": "Bear***3456", "X-Pin": "***"}


def test_redacted_values_sent_back_keep_the_credential():
    current = _policy(**{"api.example.com": {"Authorization": "Bearer sk-live-123456", "X-Pin": "1234"}})
    edited = _secrets.redact_policy(current)
    edited["allow"]["api.example.com"][0]["transform"][0]["headers"]["X-New"] = "fresh"
    edited["allow"]["other.example.com"] = [{"transform": [{"headers": {"Authorization": "Bear***3456"}}]}]

    got = _secrets.header_values(_secrets.keep_unchanged(edited, current))

    assert got["api.example.com"] == {"Authorization": "Bearer sk-live-123456", "X-Pin": "1234", "X-New": "fresh"}
    # Another domain never borrows a credential: the placeholder stays a literal.
    assert got["other.example.com"] == {"Authorization": "Bear***3456"}


def test_new_values_replace_the_credential():
    current = _policy(**{"api.example.com": {"Authorization": "Bearer old-secret-value"}})
    got = _secrets.keep_unchanged(_policy(**{"api.example.com": {"Authorization": "Bearer rotated"}}), current)
    assert _secrets.header_values(got)["api.example.com"]["Authorization"] == "Bearer rotated"

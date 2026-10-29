"""Credentials in network policies: sealed at rest, redacted on read, kept on read-modify-write."""

from __future__ import annotations

import pytest

from flint.core import _secrets
from flint.core.policy import NetworkPolicy, PolicyError


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


# ── NetworkPolicy: parsed once where it enters, read back exactly as given ──

def test_policy_reads_back_as_given_and_merges_headers_per_domain():
    raw = {"allow": {"api.example.com": [
        {"transform": [{"headers": {"Authorization": "Bearer a", "X-Pin": "1"}}], "note": "kept"},
        {"transform": [{"headers": {"Authorization": "Bearer b"}}]},
    ], "plain.example.com": [{}]}}
    policy = NetworkPolicy.parse(raw)
    assert policy.to_json() == raw
    assert policy.headers == {"api.example.com": {"Authorization": "Bearer b", "X-Pin": "1"}}
    assert policy.has_transforms
    assert not NetworkPolicy.parse({"allow": {"plain.example.com": [{}]}}).has_transforms


@pytest.mark.parametrize("raw, where", [
    ([], "must be a JSON object"),
    ({"allow": []}, "'allow' must be a JSON object"),
    ({"allow": {"a.com": {}}}, "Rules for domain 'a.com' must be a list"),
    ({"allow": {"a.com": ["x"]}}, "Each rule for domain 'a.com'"),
    ({"allow": {"a.com": [{"transform": ["x"]}]}}, "Each transform for domain 'a.com'"),
    ({"allow": {"a.com": [{"transform": [{"headers": []}]}]}}, "Transform headers for domain 'a.com'"),
    ({"allow": {"a.com": [{"transform": [{"headers": {"A": 1}}]}]}}, "Header names and values must be strings"),
])
def test_malformed_policy_is_rejected_saying_where(raw, where):
    with pytest.raises(PolicyError, match=where):
        NetworkPolicy.parse(raw)


def test_policy_edited_from_its_redacted_form_keeps_the_credential():
    current = NetworkPolicy.parse(_policy(**{"api.example.com": {"Authorization": "Bearer sk-live-123456"}}))
    edited = NetworkPolicy.parse(current.redacted()).keeping_credentials_of(current)
    assert edited.headers == current.headers

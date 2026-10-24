"""SSO config checks and who gets which access."""

from __future__ import annotations

import pytest

from flint.daemon.sso import Identity, match, parse_config

_GOOGLE = {"id": "google", "type": "google", "client_id": "c", "client_secret": "s"}


def _config(rules, providers=(_GOOGLE,)):
    return parse_config({"providers": list(providers), "rules": rules}, "http://127.0.0.1:9100")


def test_presets_fill_in_the_issuer():
    cfg = parse_config({"providers": [
        _GOOGLE,
        {"id": "entra", "type": "microsoft", "tenant": "t-123", "client_id": "c", "client_secret": "s"},
        {"id": "auth0", "type": "auth0", "domain": "acme.eu.auth0.com", "client_id": "c", "client_secret": "s"},
        {"id": "gh", "type": "github", "url": "https://ghe.acme.com", "client_id": "c", "client_secret": "s"},
    ], "rules": [{"email": "a@acme.com", "kind": "admin"}]})
    p = cfg.providers
    assert p["google"].issuer == "https://accounts.google.com"
    assert p["entra"].issuer == "https://login.microsoftonline.com/t-123/v2.0"
    assert p["auth0"].issuer == "https://acme.eu.auth0.com/"
    assert p["gh"].github_api_url == "https://ghe.acme.com/api/v3"
    assert cfg.public_url == ""  # no default given here


def test_client_secrets_can_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("GOOGLE_SECRET", "from-env")
    cfg = _config([{"email": "a@acme.com", "kind": "admin"}],
                  [{"id": "google", "type": "google", "client_id": "c", "client_secret_env": "GOOGLE_SECRET"}])
    assert cfg.providers["google"].client_secret == "from-env"


@pytest.mark.parametrize("bad, why", [
    ({"rules": [{"kind": "admin"}]}, "matches everyone"),
    ({"rules": [{"email": "a@b.c", "kind": "root"}]}, "'kind'"),
    ({"rules": [{"email": "a@b.c", "kind": "admin", "tenants": ["x"]}]}, "only service rules"),
    ({"rules": [{"email": "a@b.c", "kind": "service", "tenants": ["../x"]}]}, "invalid tenant"),
    ({"rules": [{"email": "a@b.c", "kind": "admin", "provider": "okta"}]}, "no provider"),
    ({"rules": [{"emial": "a@b.c", "kind": "admin"}]}, "unknown keys"),
    ({"rules": []}, "nobody could sign in"),
    ({"providers": [{"id": "x", "type": "auth0", "client_id": "c", "client_secret": "s"}]}, "needs 'domain'"),
    ({"providers": [{"id": "x", "type": "google", "client_id": "c"}]}, "client_secret"),
    ({"providers": [{"id": "x", "type": "ldap", "client_id": "c", "client_secret": "s"}]}, "unknown type"),
])
def test_bad_configs_say_what_is_wrong(bad, why):
    data = {"providers": [_GOOGLE], "rules": [{"email": "a@b.c", "kind": "admin"}], **bad}
    with pytest.raises(ValueError, match=why):
        parse_config(data)


def test_the_first_matching_rule_wins():
    cfg = _config([
        {"email": "boss@acme.com", "kind": "admin"},
        {"email_domain": "acme.com", "kind": "service", "tenants": ["acme"]},
    ])
    boss = Identity("google", "1", email="Boss@Acme.com", email_verified=True)
    staff = Identity("google", "2", email="dev@acme.com", email_verified=True)
    assert match(cfg.rules, boss).kind == "admin"
    assert match(cfg.rules, staff).tenants == ["acme"]
    assert match(cfg.rules, Identity("google", "3", email="x@acme.com.evil.io", email_verified=True)) is None


def test_unverified_emails_match_nothing():
    cfg = _config([{"email_domain": "acme.com", "kind": "admin"}])
    assert match(cfg.rules, Identity("google", "1", email="dev@acme.com", email_verified=False)) is None


def test_every_condition_of_a_rule_must_hold():
    gh = {"id": "github", "type": "github", "client_id": "c", "client_secret": "s"}
    cfg = _config([{"provider": "github", "github_team": "acme/platform", "kind": "admin"},
                   {"github_org": "acme", "kind": "service", "tenants": ["acme"]}], [_GOOGLE, gh])
    platform = Identity("github", "1", github_orgs=["acme"], github_teams=["acme/platform"])
    member = Identity("github", "2", github_orgs=["Acme"])
    assert match(cfg.rules, platform).kind == "admin"
    assert match(cfg.rules, member).kind == "service"
    assert match(cfg.rules, Identity("google", "3", github_teams=["acme/platform"])) is None

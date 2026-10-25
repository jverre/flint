"""Which tenant a caller acts for, and token checks."""

from __future__ import annotations

import os

import pytest

from flint.daemon.auth import ADMIN, SERVICE, AuthError, Principal, TokenStore


def test_unpinned_callers_default_to_the_default_tenant():
    assert Principal(ADMIN).resolve_tenant(None) == "default"
    assert Principal(SERVICE).resolve_tenant("") == "default"
    assert Principal(SERVICE).resolve_tenant("acme") == "acme"


def test_a_token_pinned_to_one_tenant_acts_for_it():
    p = Principal(SERVICE, ("acme",))
    assert p.resolve_tenant(None) == "acme"
    assert p.can_tenant("acme") and not p.can_tenant("globex")
    with pytest.raises(AuthError) as e:
        p.resolve_tenant("globex")
    assert e.value.status == 403


def test_a_token_pinned_to_several_tenants_must_say_which():
    p = Principal(SERVICE, ("acme", "globex"))
    with pytest.raises(AuthError) as e:
        p.resolve_tenant(None)
    assert e.value.status == 400
    assert p.resolve_tenant("globex") == "globex"


def test_tenant_names_are_checked():
    with pytest.raises(AuthError):
        Principal(ADMIN).resolve_tenant("../etc")


def test_tokens_authenticate_until_revoked(tmp_path):
    store = TokenStore(os.path.join(tmp_path, "flint.db"))
    record, token = store.create(SERVICE, "ci", ["acme"])
    assert store.authenticate(token) == Principal(SERVICE, ("acme",), record["id"], name="ci")
    assert store.authenticate(token[:-1] + ("x" if token[-1] != "x" else "y")) is None
    assert store.authenticate("not-a-token") is None
    assert all("secret" not in t and token not in str(t) for t in store.list())
    assert store.revoke(record["id"])
    assert store.authenticate(token) is None


def test_admin_tokens_take_no_tenants(tmp_path):
    with pytest.raises(AuthError):
        TokenStore(os.path.join(tmp_path, "flint.db")).create(ADMIN, "ops", ["acme"])


def _authenticates(path: str, *, over_socket: bool) -> "Principal | None | str":
    """Run the auth middleware's decision for a GET to ``path`` with no token.
    Returns the principal, or the refusal status as a string."""
    from flint.daemon.auth import AuthMiddleware, AuthError
    mw = AuthMiddleware(None, tokens=lambda: None, cluster_mode=lambda: False, vm_tenant=lambda _id: None)
    server = ("/run/flint.sock", None) if over_socket else ("127.0.0.1", 9100)
    scope = {"type": "http", "method": "GET", "path": path, "headers": [], "server": server}
    try:
        return mw._authenticate(scope, path)
    except AuthError as e:
        return f"{e.status}"


def test_public_paths_need_no_token():
    # The genuinely public surface stays reachable without a token.
    for path in ("/health", "/ui/", "/ui/assets/app.js", "/auth/login/google"):
        assert _authenticates(path, over_socket=False) is None, path


def test_a_dot_segment_never_makes_a_path_public():
    # A TCP caller can't dodge the token check by hiding an admin route behind
    # the public /ui/ prefix with a dot segment (decoded by the server by now).
    for path in ("/ui/../config", "/ui/../v1/tokens", "/ui/../../config", "/health/../config"):
        assert _authenticates(path, over_socket=False) == "401", path
    # Over the trusted Unix socket the caller is an admin regardless.
    assert _authenticates("/ui/../config", over_socket=True).is_admin

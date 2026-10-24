"""Signing in to the daemon with an identity provider (SSO).

conftest.py runs a real OpenID provider (oidc-provider-mock) and configures
the test daemon with it: admin@flint.test is an admin, the acme-support
group gets service access to tenant acme, and nobody else gets in.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from urllib.parse import urlsplit

import httpx
import pytest

from . import conftest
from .conftest import TCP_URL


@pytest.fixture
def idp(daemon_only):
    if not conftest.MOCK_IDP:
        pytest.skip("no mock identity provider (oidc-provider-mock not installed, or the daemon was already running)")
    providers = httpx.get(f"{TCP_URL}/auth/providers", timeout=5).json()["providers"]
    assert [p["id"] for p in providers] == ["mock"], providers
    return conftest.MOCK_IDP


def _person(idp: str, **claims) -> str:
    sub = f"user-{uuid.uuid4().hex[:8]}"
    httpx.put(f"{idp}/users/{sub}", json=claims, timeout=5).raise_for_status()
    return sub


def _browser_sign_in(sub: str, next_path: str = "/ui/vms") -> tuple[httpx.Client, httpx.Response]:
    """What a browser does: Flint -> provider (the person picks their account) -> Flint."""
    browser = httpx.Client(base_url=TCP_URL, timeout=10)
    to_idp = browser.get("/auth/login/mock", params={"next": next_path})
    assert to_idp.status_code == 302, to_idp.text
    back = httpx.post(to_idp.headers["location"], data={"sub": sub}, timeout=10)
    assert back.status_code == 302, back.text[:500]
    cb = urlsplit(back.headers["location"])
    assert f"{cb.scheme}://{cb.netloc}" == TCP_URL and cb.path == "/auth/callback/mock", back.headers["location"]
    return browser, browser.get(f"{cb.path}?{cb.query}")


def test_an_admin_signs_in_and_uses_the_api(idp):
    sub = _person(idp, email="admin@flint.test", email_verified=True)
    browser, done = _browser_sign_in(sub)
    assert done.status_code == 302 and done.headers["location"] == "/ui/vms", done.text
    assert "httponly" in done.headers["set-cookie"].lower()
    me = browser.get("/auth/me").json()
    assert (me["identity"], me["kind"], me["tenants"]) == ("mock:admin@flint.test", "admin", [])
    assert browser.get("/vms").status_code == 200
    assert browser.get("/config").status_code == 200
    # The session is a token like any other: an admin can see and revoke it.
    tokens = browser.get("/v1/tokens").json()["tokens"]
    assert any(t["identity"] == "mock:admin@flint.test" and t["expires_at"] for t in tokens), tokens


def test_a_group_rule_gives_one_tenant(idp):
    sub = _person(idp, email="support@other.test", email_verified=True, groups=["acme-support"])
    browser, done = _browser_sign_in(sub)
    assert done.status_code == 302, done.text
    me = browser.get("/auth/me").json()
    assert (me["kind"], me["tenants"]) == ("service", ["acme"]), me
    assert browser.get("/config").status_code == 403
    assert all(v["tenant"] == "acme" for v in browser.get("/vms").json()["vms"])


def test_people_no_rule_matches_get_no_access(idp):
    for claims in ({"email": "stranger@other.test", "email_verified": True},
                   {"email": "admin@flint.test", "email_verified": False}):  # an unverified address proves nothing
        browser, done = _browser_sign_in(_person(idp, **claims))
        assert done.status_code == 403, (claims, done.text)
        assert "flint_session" not in done.headers.get("set-cookie", "")
        assert browser.get("/vms").status_code == 401


def test_a_session_refuses_changes_from_other_sites(idp):
    browser, _ = _browser_sign_in(_person(idp, email="admin@flint.test", email_verified=True))
    evil = {"Origin": "https://evil.example"}
    assert browser.post("/v1/tokens", json={"kind": "admin"}, headers=evil).status_code == 403
    assert browser.get("/vms", headers=evil).status_code == 200  # reading isn't a change
    made = browser.post("/v1/tokens", json={"kind": "service", "name": "from the UI"}, headers={"Origin": TCP_URL})
    assert made.status_code == 200, made.text
    browser.delete(f"/v1/tokens/{made.json()['id']}", headers={"Origin": TCP_URL})


def test_signing_out_ends_the_session(idp):
    browser, _ = _browser_sign_in(_person(idp, email="admin@flint.test", email_verified=True))
    cookie = browser.cookies.get("flint_session")
    assert browser.post("/auth/logout", headers={"Origin": TCP_URL}).status_code == 200
    stolen = httpx.get(f"{TCP_URL}/vms", cookies={"flint_session": cookie}, timeout=5)
    assert stolen.status_code == 401, "the session still works after signing out"


def test_sign_in_only_returns_to_this_server(idp):
    browser, done = _browser_sign_in(_person(idp, email="admin@flint.test", email_verified=True),
                                     next_path="//evil.example/steal")
    assert done.headers["location"] == "/ui/", done.headers["location"]


def test_cli_signs_in_through_the_browser(idp, tmp_path):
    """`flint login`: it prints a URL, the person signs in in their browser,
    and the CLI ends up with a token it then uses."""
    sub = _person(idp, email="admin@flint.test", email_verified=True)
    flint = os.path.join(os.path.dirname(sys.executable), "flint")
    env = {**os.environ, "FLINT_URL": TCP_URL, "FLINT_CREDENTIALS": str(tmp_path / "credentials.json"), "PYTHONUNBUFFERED": "1"}
    env.pop("FLINT_API_KEY", None)
    cli = subprocess.Popen([flint, "login", "--no-open"], env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        line = cli.stdout.readline()
        assert line.startswith("Sign in at: "), line + cli.stdout.read()
        browser = httpx.Client(timeout=10)
        to_idp = browser.get(line.split(": ", 1)[1].strip())
        back = httpx.post(to_idp.headers["location"], data={"sub": sub}, timeout=10)
        to_cli = browser.get(back.headers["location"])  # Flint's callback sends the browser to the CLI
        assert to_cli.status_code == 302 and to_cli.headers["location"].startswith("http://127.0.0.1:"), to_cli.text
        assert browser.get(to_cli.headers["location"]).status_code == 200
        out, _ = cli.communicate(timeout=30)
    finally:
        cli.kill()
    assert cli.returncode == 0 and "Signed in" in out, out
    assert oct((tmp_path / "credentials.json").stat().st_mode & 0o777) == "0o600"
    listed = subprocess.run([flint, "list"], env=env, capture_output=True, text=True, timeout=60)
    assert listed.returncode == 0, listed.stdout + listed.stderr
    out = subprocess.run([flint, "logout"], env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stdout + out.stderr
    listed = subprocess.run([flint, "list"], env=env, capture_output=True, text=True, timeout=60)
    assert listed.returncode != 0, "still signed in after `flint logout`"

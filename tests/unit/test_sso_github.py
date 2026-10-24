"""Signing in with GitHub, against a fake GitHub (OAuth endpoints and the API calls Flint makes).

GitHub isn't OpenID Connect: Flint reads who the person is, their verified
email and their orgs and teams from the API. Runs the real app in-process.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

_PEOPLE = {
    "lead": {"user": {"id": 1, "login": "lead"}, "emails": [{"email": "lead@acme.com", "primary": True, "verified": True}],
             "orgs": [{"login": "acme"}], "teams": [{"slug": "platform", "organization": {"login": "acme"}}]},
    "dev": {"user": {"id": 2, "login": "dev"}, "emails": [{"email": "dev@gmail.com", "primary": True, "verified": True}],
            "orgs": [{"login": "acme"}], "teams": []},
    "outsider": {"user": {"id": 3, "login": "outsider"}, "emails": [], "orgs": [{"login": "other"}], "teams": []},
}


class _FakeGitHub(BaseHTTPRequestHandler):
    codes: dict[str, str] = {}

    def _json(self, body, status=200):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == "/login/oauth/authorize":  # the person approves; the test says who they are
            q = parse_qs(url.query)
            code = f"code-{q['login'][0]}"
            self.codes[code] = q["login"][0]
            self.send_response(302)
            self.send_header("Location", f"{q['redirect_uri'][0]}?{urlencode({'code': code, 'state': q['state'][0]})}")
            self.end_headers()
            return
        login = self.headers.get("Authorization", "").removeprefix("Bearer tok-")
        person = _PEOPLE.get(login)
        if person is None:
            return self._json({"message": "Bad credentials"}, 401)
        key = {"/api/user": "user", "/api/user/emails": "emails", "/api/user/orgs": "orgs", "/api/user/teams": "teams"}.get(url.path)
        self._json(person[key] if key else {"message": "Not Found"}, 200 if key else 404)

    def do_POST(self):
        form = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
        login = self.codes.pop(form["code"][0], None)
        assert form.get("code_verifier"), "Flint must use PKCE"
        self._json({"access_token": f"tok-{login}", "token_type": "bearer", "scope": "read:user,user:email,read:org"})

    def log_message(self, *args):
        pass


@pytest.fixture
def flint(tmp_path):
    gh = HTTPServer(("127.0.0.1", 0), _FakeGitHub)
    threading.Thread(target=gh.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{gh.server_port}"

    import flint.daemon.server as srv
    from flint.daemon import sso
    from flint.daemon.auth import TokenStore

    config = sso.parse_config({
        "public_url": "http://testserver",
        "providers": [{"id": "github", "type": "github", "url": base, "api_url": f"{base}/api",
                       "client_id": "c", "client_secret": "s"}],
        "rules": [{"github_team": "acme/platform", "kind": "admin"},
                  {"github_org": "acme", "kind": "service", "tenants": ["acme"]}],
    })

    class _Daemon:
        cluster = None
        manager = None
        tokens = TokenStore(str(tmp_path / "flint.db"))

    srv.app.state.daemon, srv.app.state.sso = _Daemon(), sso.SSO(config)
    if not any(m.cls is SessionMiddleware for m in srv.app.user_middleware):
        srv.app.add_middleware(SessionMiddleware, secret_key="test", session_cookie=sso.LOGIN_COOKIE)
    yield srv.app
    gh.shutdown()
    del srv.app.state.sso


def _sign_in(app, login: str):
    browser = TestClient(app, follow_redirects=False)
    to_github = urlsplit(browser.get("/auth/login/github").headers["location"])
    import httpx
    back = httpx.get(f"{to_github.scheme}://{to_github.netloc}{to_github.path}?{to_github.query}&login={login}")
    cb = urlsplit(back.headers["location"])
    return browser, browser.get(f"{cb.path}?{cb.query}")


def test_github_teams_and_orgs_decide_access(flint):
    lead, done = _sign_in(flint, "lead")
    assert done.status_code == 302, done.text
    me = lead.get("/auth/me").json()
    assert (me["identity"], me["kind"]) == ("github:lead@acme.com", "admin"), me

    dev, done = _sign_in(flint, "dev")
    assert dev.get("/auth/me").json()["tenants"] == ["acme"]

    outsider, done = _sign_in(flint, "outsider")
    assert done.status_code == 403 and outsider.get("/auth/me").status_code == 401

"""Sign in to Flint with an identity provider (SSO).

This is for the people who run Flint (the web UI and the CLI), not for the
users of your app: your backend keeps using service tokens.

Configured by a JSON file, ``FLINT_SSO_CONFIG`` (default ``STATE_DIR/sso.json``)::

    {
      "public_url": "https://flint.example.com",
      "session_hours": 12,
      "providers": [
        {"id": "google", "type": "google", "client_id": "...", "client_secret_env": "GOOGLE_SECRET"},
        {"id": "github", "type": "github", "client_id": "...", "client_secret_env": "GITHUB_SECRET"},
        {"id": "auth0", "type": "auth0", "domain": "acme.eu.auth0.com", "client_id": "...",
         "client_secret_env": "AUTH0_SECRET", "groups_claim": "https://flint/groups"}
      ],
      "rules": [
        {"email": "alice@acme.com", "kind": "admin"},
        {"github_team": "acme/platform", "kind": "admin"},
        {"group": "acme-support", "kind": "service", "tenants": ["acme"]}
      ]
    }

Provider types: ``oidc`` (any OpenID Connect provider, by ``issuer``) and
presets for ``google``, ``microsoft`` (``tenant``), ``auth0``, ``okta``
(``domain``) and ``gitlab`` (``url`` for self-hosted); ``github`` (OAuth 2,
``url``/``api_url`` for GitHub Enterprise). Sign-in uses the authorization
code flow with PKCE, state and nonce (Authlib), and ID tokens are verified
against the provider's keys.

Rules grant access: the first rule whose every condition holds gives
``admin``, or ``service`` access pinned to ``tenants``. Conditions:
``provider``, ``email`` and ``email_domain`` (verified emails only),
``group`` (in the provider's ``groups_claim``), ``github_org`` and
``github_team`` (``org/team``). Nobody matches no rule gets in.

A sign-in becomes an API token that expires after ``session_hours``: a
browser keeps it in an HttpOnly cookie, ``flint login`` in the user's
credentials file. Sessions show up, and can be revoked, with the tokens.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from flint.core.config import DAEMON_PORT, STATE_DIR, log
from flint.daemon.auth import ADMIN, SERVICE, SESSION_COOKIE, AuthError, _TENANT_RE, session_cookie

CONFIG_PATH = os.environ.get("FLINT_SSO_CONFIG") or os.path.join(STATE_DIR, "sso.json")
LOGIN_COOKIE = "flint_login"  # Authlib's state, nonce and PKCE verifier, for the sign-in only

_ISSUERS = {
    "google": lambda p: "https://accounts.google.com",
    "microsoft": lambda p: f"https://login.microsoftonline.com/{_need(p, 'tenant')}/v2.0",
    "auth0": lambda p: f"https://{_need(p, 'domain')}/",
    "okta": lambda p: f"https://{_need(p, 'domain')}",
    "gitlab": lambda p: p.get("url") or "https://gitlab.com",
    "oidc": lambda p: _need(p, "issuer"),
}
_LABELS = {"google": "Google", "microsoft": "Microsoft", "auth0": "Auth0", "okta": "Okta",
           "gitlab": "GitLab", "github": "GitHub", "oidc": "SSO"}
_MATCHERS = ("provider", "email", "email_domain", "group", "github_org", "github_team")


def _need(p: dict, key: str) -> str:
    if not p.get(key):
        raise ValueError(f"provider {p.get('id')!r} ({p.get('type')}) needs {key!r}")
    return p[key]


@dataclass
class Provider:
    id: str
    type: str
    label: str
    client_id: str
    client_secret: str
    issuer: str = ""  # OIDC
    scopes: list[str] = field(default_factory=list)
    groups_claim: str = "groups"
    github_url: str = "https://github.com"
    github_api_url: str = "https://api.github.com"


@dataclass
class Rule:
    kind: str
    tenants: list[str] = field(default_factory=list)
    provider: str = ""
    email: str = ""
    email_domain: str = ""
    group: str = ""
    github_org: str = ""
    github_team: str = ""


@dataclass
class Identity:
    provider: str
    subject: str
    email: str = ""
    email_verified: bool = False
    groups: list[str] = field(default_factory=list)
    github_orgs: list[str] = field(default_factory=list)
    github_teams: list[str] = field(default_factory=list)  # "org/team"
    login: str = ""

    @property
    def name(self) -> str:
        return f"{self.provider}:{self.email or self.login or self.subject}"


@dataclass
class Config:
    public_url: str
    session_hours: float
    providers: dict[str, Provider]
    rules: list[Rule]


def parse_config(data: dict, default_public_url: str = "") -> Config:
    """Check and normalise an SSO config. Raises ValueError with what's wrong."""
    if not isinstance(data, dict):
        raise ValueError("the SSO config must be a JSON object")
    providers: dict[str, Provider] = {}
    for p in data.get("providers") or []:
        pid, ptype = p.get("id") or p.get("type"), p.get("type")
        if not pid or not isinstance(pid, str) or not pid.replace("-", "").replace("_", "").isalnum():
            raise ValueError(f"provider id {pid!r} must be letters, digits, '-' or '_'")
        if pid in providers:
            raise ValueError(f"provider {pid!r} is defined twice")
        if ptype not in (*_ISSUERS, "github"):
            raise ValueError(f"provider {pid!r}: unknown type {ptype!r}; use one of {', '.join([*_ISSUERS, 'github'])}")
        secret = p.get("client_secret") or os.environ.get(p.get("client_secret_env") or "", "")
        if not p.get("client_id") or not secret:
            raise ValueError(f"provider {pid!r} needs client_id and client_secret (or client_secret_env, set)")
        prov = Provider(id=pid, type=ptype, label=p.get("label") or _LABELS[ptype], client_id=p["client_id"],
                        client_secret=secret, groups_claim=p.get("groups_claim") or "groups")
        if ptype == "github":
            prov.github_url = (p.get("url") or prov.github_url).rstrip("/")
            prov.github_api_url = (p.get("api_url") or (prov.github_api_url if not p.get("url") else f"{prov.github_url}/api/v3")).rstrip("/")
            prov.scopes = p.get("scopes") or ["read:user", "user:email", "read:org"]
        else:
            prov.issuer = _ISSUERS[ptype]({**p, "id": pid})
            prov.scopes = p.get("scopes") or ["openid", "email", "profile"]
            if "openid" not in prov.scopes:
                raise ValueError(f"provider {pid!r}: scopes must include 'openid'")
        providers[pid] = prov
    rules = []
    for i, r in enumerate(data.get("rules") or []):
        unknown = set(r) - {*_MATCHERS, "kind", "tenants"}
        if unknown:
            raise ValueError(f"rule {i + 1}: unknown keys {sorted(unknown)}")
        if not isinstance(r.get("tenants", []), list):
            raise ValueError(f"rule {i + 1}: 'tenants' must be a list")
        rule = Rule(**{"kind": r.get("kind", ""), **{k: v for k, v in r.items() if k != "kind"}})
        if rule.kind not in (ADMIN, SERVICE):
            raise ValueError(f"rule {i + 1}: 'kind' must be 'admin' or 'service'")
        if rule.kind == ADMIN and rule.tenants:
            raise ValueError(f"rule {i + 1}: admins act for every tenant; only service rules take 'tenants'")
        if not any(getattr(rule, m) for m in _MATCHERS if m != "provider"):
            raise ValueError(f"rule {i + 1} matches everyone; give it an email, email_domain, group, "
                             "github_org or github_team")
        if rule.provider and rule.provider not in providers:
            raise ValueError(f"rule {i + 1}: no provider {rule.provider!r}")
        for t in rule.tenants:
            if not isinstance(t, str) or not _TENANT_RE.match(t):
                raise ValueError(f"rule {i + 1}: invalid tenant {t!r}")
        rules.append(rule)
    if providers and not rules:
        raise ValueError("there are providers but no rules, so nobody could sign in")
    public_url = (data.get("public_url") or default_public_url).rstrip("/")
    return Config(public_url=public_url, session_hours=float(data.get("session_hours") or 12),
                  providers=providers, rules=rules)


def match(rules: list[Rule], who: Identity) -> Rule | None:
    """The first rule ``who`` meets, or None."""
    email = who.email.lower() if who.email_verified else ""
    for r in rules:
        if r.provider and r.provider != who.provider:
            continue
        if r.email and r.email.lower() != email:
            continue
        if r.email_domain and not (email and email.rsplit("@", 1)[-1] == r.email_domain.lower().lstrip("@")):
            continue
        if r.group and r.group not in who.groups:
            continue
        if r.github_org and r.github_org.lower() not in (o.lower() for o in who.github_orgs):
            continue
        if r.github_team and r.github_team.lower() not in (t.lower() for t in who.github_teams):
            continue
        return r
    return None


# ── Runtime ──────────────────────────────────────────────────────────────

class SSO:
    def __init__(self, config: Config) -> None:
        from authlib.integrations.starlette_client import OAuth
        self.config = config
        self.oauth = OAuth()
        for p in config.providers.values():
            kwargs = {"scope": " ".join(p.scopes), "code_challenge_method": "S256"}
            if p.type == "github":
                self.oauth.register(
                    p.id, client_id=p.client_id, client_secret=p.client_secret,
                    authorize_url=f"{p.github_url}/login/oauth/authorize",
                    access_token_url=f"{p.github_url}/login/oauth/access_token",
                    api_base_url=p.github_api_url + "/", client_kwargs=kwargs,
                )
            else:
                self.oauth.register(
                    p.id, client_id=p.client_id, client_secret=p.client_secret,
                    server_metadata_url=f"{p.issuer.rstrip('/')}/.well-known/openid-configuration",
                    client_kwargs=kwargs,
                )
        self._cli_codes: dict[str, tuple[str, str, float, dict]] = {}  # code -> token, challenge, expiry, info
        self._lock = threading.Lock()

    def redirect_uri(self, pid: str) -> str:
        return f"{self.config.public_url}/auth/callback/{pid}"

    @property
    def secure_cookies(self) -> bool:
        return self.config.public_url.startswith("https://")

    def hold_for_cli(self, token: str, challenge: str, info: dict) -> str:
        code = secrets.token_urlsafe(24)
        with self._lock:
            now = time.time()
            self._cli_codes = {k: v for k, v in self._cli_codes.items() if v[2] > now}
            self._cli_codes[code] = (token, challenge, now + 120, info)
        return code

    def take_for_cli(self, code: str, verifier: str) -> tuple[str, dict] | None:
        with self._lock:
            held = self._cli_codes.pop(code, None)  # one try per code
        if not held or held[2] < time.time():
            return None
        token, challenge, _exp, info = held
        if not secrets.compare_digest(challenge, pkce_challenge(verifier)):
            return None
        return token, info


def pkce_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def load(default_public_url: str = f"http://127.0.0.1:{DAEMON_PORT}") -> SSO | None:
    """The configured SSO, or None when there's no config file."""
    try:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"can't read the SSO config {CONFIG_PATH}: {e}") from e
    config = parse_config(data, default_public_url)
    return SSO(config) if config.providers else None


async def _github_identity(client, pid: str) -> Identity:
    user = (await client.get("user")).json()
    emails = (await client.get("user/emails")).json()
    primary = next((e for e in emails if isinstance(e, dict) and e.get("primary") and e.get("verified")), None)
    orgs = (await client.get("user/orgs", params={"per_page": 100})).json()
    teams = (await client.get("user/teams", params={"per_page": 100})).json()
    return Identity(
        provider=pid, subject=str(user["id"]), login=user.get("login", ""),
        email=(primary or {}).get("email", ""), email_verified=bool(primary),
        github_orgs=[o["login"] for o in orgs if isinstance(o, dict) and "login" in o],
        github_teams=[f"{t['organization']['login']}/{t['slug']}" for t in teams
                      if isinstance(t, dict) and "slug" in t and "organization" in t],
    )


def _oidc_identity(p: Provider, claims: dict) -> Identity:
    groups = claims.get(p.groups_claim) or []
    return Identity(
        provider=p.id, subject=str(claims["sub"]), email=str(claims.get("email") or ""),
        email_verified=claims.get("email_verified") is True or claims.get("email_verified") == "true",
        groups=[str(g) for g in groups] if isinstance(groups, list) else [str(groups)],
        login=str(claims.get("preferred_username") or ""),
    )


def _page(title: str, text: str, status: int) -> HTMLResponse:
    return HTMLResponse(
        f'<!doctype html><meta charset="utf-8"><title>{html.escape(title)}</title>'
        '<body style="font:14px system-ui;background:#151922;color:#eef1f7;display:grid;place-items:center;height:100vh;margin:0">'
        f'<div style="max-width:420px"><h1 style="font-size:18px">{html.escape(title)}</h1><p>{html.escape(text)}</p>'
        '<p><a style="color:#9cc1ff" href="/ui/">Back to Flint</a></p></div>', status_code=status)


def _safe_next(path: str | None) -> str:
    """Only paths on this server, so the sign-in can't be used to redirect elsewhere."""
    if not path or not path.startswith("/") or path.startswith("//") or "\\" in path:
        return "/ui/"
    return path


router = APIRouter(prefix="/auth")


def _sso(request: Request) -> SSO:
    sso = getattr(request.app.state, "sso", None)
    if sso is None:
        raise HTTPException(status_code=404, detail="SSO isn't configured")
    return sso


def _tokens(request: Request):
    return request.app.state.daemon.tokens


@router.get("/providers")
def providers(request: Request):
    sso = getattr(request.app.state, "sso", None)
    ps = sso.config.providers.values() if sso else []
    return {"providers": [{"id": p.id, "label": p.label, "type": p.type} for p in ps]}


@router.get("/login/{pid}")
async def login(pid: str, request: Request, next: str = "", cli_port: int = 0, cli_challenge: str = ""):
    """Start signing in with provider ``pid``. ``flint login`` passes the
    port it listens on and a PKCE challenge for the token it will claim."""
    sso = _sso(request)
    if pid not in sso.config.providers:
        raise HTTPException(status_code=404, detail=f"No provider {pid!r}")
    if cli_port and not (1024 <= cli_port <= 65535 and 20 <= len(cli_challenge) <= 128):
        raise HTTPException(status_code=400, detail="Bad cli_port or cli_challenge")
    request.session["flint"] = {"next": _safe_next(next), "cli_port": cli_port, "cli_challenge": cli_challenge}
    return await sso.oauth.create_client(pid).authorize_redirect(request, sso.redirect_uri(pid))


@router.get("/callback/{pid}")
async def callback(pid: str, request: Request):
    from authlib.integrations.base_client import OAuthError
    sso = _sso(request)
    p = sso.config.providers.get(pid)
    if p is None:
        raise HTTPException(status_code=404, detail=f"No provider {pid!r}")
    pending = request.session.pop("flint", None)
    if pending is None:
        return _page("Sign-in expired", "Start again from Flint.", 400)
    client = sso.oauth.create_client(pid)
    try:
        token = await client.authorize_access_token(request)
        if p.type == "github":
            who = await _github_identity(_GitHubAPI(client, token), pid)
        else:
            if "userinfo" not in token:  # authorize_access_token verified the ID token if there was one
                return _page("Sign-in failed", f"{p.label} returned no ID token.", 502)
            who = _oidc_identity(p, dict(token["userinfo"]))
    except OAuthError as e:
        log.warning("SSO %s: sign-in failed: %s", pid, e)
        return _page("Sign-in failed", f"{p.label} said: {e.description or e.error}", 400)
    rule = match(sso.config.rules, who)
    if rule is None:
        log.warning("SSO %s: %s has no access (no rule matches)", pid, who.name)
        shown = who.email or who.login or who.subject
        return _page("No access", f"{shown} isn't allowed to use this Flint. Ask its admin to add a rule for you.", 403)
    ttl = sso.config.session_hours * 3600
    record, api_token = _tokens(request).create(
        rule.kind, f"sign-in {who.name}", rule.tenants, ttl=ttl, identity=who.name)
    log.info("SSO %s: %s signed in as %s%s", pid, who.name, rule.kind,
             f" for {', '.join(rule.tenants)}" if rule.tenants else "")
    if pending.get("cli_port"):
        info = {"kind": rule.kind, "tenants": rule.tenants, "identity": who.name, "expires_at": record["expires_at"]}
        code = sso.hold_for_cli(api_token, pending["cli_challenge"], info)
        return RedirectResponse(f"http://127.0.0.1:{pending['cli_port']}/callback?{urlencode({'code': code})}", 302)
    resp = RedirectResponse(pending["next"], 302)
    resp.set_cookie(SESSION_COOKIE, api_token, max_age=int(ttl), httponly=True, samesite="lax",
                    secure=sso.secure_cookies, path="/")
    return resp


class _GitHubAPI:
    """GET on GitHub's API with the user's token, raising on errors."""

    def __init__(self, client, token) -> None:
        self._client, self._token = client, token

    async def get(self, path: str, **kwargs):
        resp = await self._client.get(path, token=self._token, headers={"Accept": "application/vnd.github+json"}, **kwargs)
        resp.raise_for_status()
        return resp


@router.post("/cli/exchange")
def cli_exchange(body: dict, request: Request):
    """``flint login`` trades the one-time code it was sent, plus the PKCE
    verifier only it knows, for its token."""
    held = _sso(request).take_for_cli(str(body.get("code") or ""), str(body.get("verifier") or ""))
    if held is None:
        raise HTTPException(status_code=400, detail="Invalid or expired sign-in code")
    token, info = held
    return {"token": token, **info}


def _caller_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    return auth[7:].strip() if auth[:7].lower() == "bearer " else session_cookie(request.scope)


@router.get("/me")
def me(request: Request):
    """Who the caller is: the web UI shows it, and uses it to check a session."""
    principal = _tokens(request).authenticate(_caller_token(request) or "")
    if principal is None:
        raise HTTPException(status_code=401, detail="Not signed in")
    return {"kind": principal.kind, "tenants": list(principal.tenants), "identity": principal.identity,
            "token_id": principal.token_id}


@router.post("/logout")
def logout(request: Request):
    """End this browser's (or CLI's) session: its token is revoked."""
    from flint.daemon.auth import _same_origin
    if not request.headers.get("authorization") and not _same_origin(request.scope):
        raise AuthError(403, "Cross-origin request refused")
    principal = _tokens(request).authenticate(_caller_token(request) or "")
    if principal is not None and principal.identity:  # only sign-in sessions; API tokens are revoked by an admin
        _tokens(request).revoke(principal.token_id)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp

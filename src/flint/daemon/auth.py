"""Who may call the daemon, and which tenants they may act for.

- The Unix socket (``DAEMON_SOCKET``) is trusted: whoever can open it is an
  admin. Only the daemon's user (and the ``flint`` group) can.
- TCP needs an API token, ``Authorization: Bearer flint_<id>_<secret>``
  (``?token=`` on WebSockets). Only a hash of the secret is stored.
  ``admin`` tokens can do everything. ``service`` tokens are for the
  builder's backend: VMs, volumes and templates, but not daemon config or
  tokens. A service token pinned to tenants sees only their resources;
  others' look like they don't exist.
- In a cluster, the daemon is a host agent behind mTLS: the control plane
  is the only caller and enforces tenancy itself.

A tenant is an isolation group, not an identity: the caller says which
tenant a resource belongs to, and Flint keeps tenants apart.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass, field

from flint.core.config import ADMIN_TOKEN_PATH, log

ADMIN = "admin"
SERVICE = "service"
DEFAULT_TENANT = "default"

_TENANT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS api_tokens (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    secret_hash TEXT NOT NULL,
    tenants     TEXT NOT NULL DEFAULT '[]',
    name        TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL
);
"""


class AuthError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class Principal:
    kind: str
    tenants: tuple[str, ...] = field(default_factory=tuple)  # empty: any tenant
    token_id: str = ""  # "" for the socket and the cluster's control plane

    @property
    def is_admin(self) -> bool:
        return self.kind == ADMIN

    def can_tenant(self, tenant: str) -> bool:
        return self.is_admin or not self.tenants or (tenant or DEFAULT_TENANT) in self.tenants

    def resolve_tenant(self, requested: str | None) -> str:
        """The tenant a new resource belongs to: the one asked for if allowed,
        else the caller's only tenant, else ``default``."""
        if requested:
            if not _TENANT_RE.match(requested):
                raise AuthError(400, "'tenant' must be 1-63 letters, digits, '.', '_' or '-'")
            if not self.can_tenant(requested):
                raise AuthError(403, f"This token can't act for tenant '{requested}'")
            return requested
        if self.tenants and not self.is_admin:
            if len(self.tenants) == 1:
                return self.tenants[0]
            raise AuthError(400, f"'tenant' is required: this token acts for {', '.join(self.tenants)}")
        return DEFAULT_TENANT


TRUSTED = Principal(ADMIN)


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _format(token_id: str, secret: str) -> str:
    return f"flint_{token_id}_{secret}"


def _parse(token: str) -> tuple[str, str] | None:
    if not token.startswith("flint_"):
        return None
    token_id, sep, secret = token[len("flint_"):].partition("_")
    return (token_id, secret) if token_id and sep and secret else None


class TokenStore:
    """API tokens, in the daemon's database (own connection: WAL allows it)."""

    def __init__(self, db_path: str) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def create(self, kind: str, name: str = "", tenants: list[str] | None = None) -> tuple[dict, str]:
        """Returns the token's record and its string form, which is shown once."""
        if kind not in (ADMIN, SERVICE):
            raise AuthError(400, "'kind' must be 'admin' or 'service'")
        tenants = list(tenants or [])
        if kind == ADMIN and tenants:
            raise AuthError(400, "Admin tokens act for every tenant; only service tokens take 'tenants'")
        for t in tenants:
            if not isinstance(t, str) or not _TENANT_RE.match(t):
                raise AuthError(400, f"Invalid tenant {t!r}")
        token_id = secrets.token_hex(6)
        secret = secrets.token_urlsafe(24).replace("_", "x")
        record = {"id": token_id, "kind": kind, "tenants": tenants, "name": name or "", "created_at": time.time()}
        with self._lock:
            self._conn.execute(
                "INSERT INTO api_tokens (id, kind, secret_hash, tenants, name, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (token_id, kind, _hash(secret), json.dumps(tenants), record["name"], record["created_at"]),
            )
            self._conn.commit()
        return record, _format(token_id, secret)

    def list(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, kind, tenants, name, created_at FROM api_tokens ORDER BY created_at"
            ).fetchall()
        return [{"id": r[0], "kind": r[1], "tenants": json.loads(r[2]), "name": r[3], "created_at": r[4]} for r in rows]

    def revoke(self, token_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM api_tokens WHERE id = ?", (token_id,))
            self._conn.commit()
        return cur.rowcount > 0

    def authenticate(self, token: str) -> Principal | None:
        parsed = _parse(token)
        if not parsed:
            return None
        token_id, secret = parsed
        with self._lock:
            row = self._conn.execute(
                "SELECT kind, secret_hash, tenants FROM api_tokens WHERE id = ?", (token_id,)
            ).fetchone()
        if not row or not hmac.compare_digest(row[1], _hash(secret)):
            return None
        return Principal(row[0], tuple(json.loads(row[2])), token_id)

    def ensure_admin_token_file(self, path: str = ADMIN_TOKEN_PATH) -> None:
        """Keep a working admin token in ``path`` (0600) for TCP clients like the web UI."""
        try:
            with open(path) as f:
                if self.authenticate(f.read().strip()):
                    return
        except OSError:
            pass
        _record, token = self.create(ADMIN, name="local admin")
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(token + "\n")
        os.replace(tmp, path)
        log.info("Wrote a new admin token to %s", path)


# ── Request authentication (ASGI middleware) ─────────────────────────────

# Reachable without a token: liveness and the web UI's static files (the
# UI then signs in with a token).
_PUBLIC = re.compile(r"^/(health|ui(/.*)?)?$")
_ADMIN_ONLY = re.compile(r"^/(config|v1/tokens)(/.*)?$")
_VM_PATH = re.compile(r"^/vms/([^/]+)(/.*)?$")


def _bearer(scope) -> str:
    for k, v in scope.get("headers") or []:
        if k == b"authorization":
            value = v.decode("latin-1").strip()
            if value[:7].lower() == "bearer ":
                return value[7:].strip()
    from urllib.parse import parse_qs
    return (parse_qs((scope.get("query_string") or b"").decode()).get("token") or [""])[0]


def _over_unix_socket(scope) -> bool:
    """uvicorn reports a Unix socket's server address as (path, None), or None
    in older versions; a TCP one always has a port."""
    server = scope.get("server")
    return server is None or server[1] is None


class AuthMiddleware:
    """Authenticates every request and keeps callers to their tenants' VMs.

    ``vm_tenant(vm_id)`` returns a VM's tenant, or None if there's no such VM;
    a VM of a tenant the caller can't act for answers 404, as if it didn't exist.
    """

    def __init__(self, app, *, tokens, cluster_mode, vm_tenant) -> None:
        self.app = app
        self._tokens = tokens  # callable -> TokenStore | None
        self._cluster_mode = cluster_mode  # callable -> bool
        self._vm_tenant = vm_tenant

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        try:
            principal = self._authenticate(scope, path)
        except AuthError as e:
            return await self._refuse(scope, receive, send, e)
        scope.setdefault("state", {})["principal"] = principal
        return await self.app(scope, receive, send)

    def _authenticate(self, scope, path: str) -> Principal | None:
        if _PUBLIC.match(path):
            return None
        if self._cluster_mode() or _over_unix_socket(scope):
            principal = TRUSTED  # mTLS from the control plane, or the Unix socket
        else:
            token = _bearer(scope)
            store = self._tokens()
            principal = store.authenticate(token) if (token and store) else None
            if principal is None:
                raise AuthError(401, "Missing or invalid API token (use the daemon's socket, "
                                     "or create a token with `flint token create`)")
        if _ADMIN_ONLY.match(path) and not principal.is_admin:
            raise AuthError(403, "Admin token required")
        m = _VM_PATH.match(path)
        if m and not principal.is_admin and principal.tenants:
            tenant = self._vm_tenant(m.group(1))
            if tenant is not None and not principal.can_tenant(tenant):
                raise AuthError(404, "VM not found")
        return principal

    @staticmethod
    async def _refuse(scope, receive, send, e: AuthError) -> None:
        if scope["type"] == "websocket":
            # Closing before accepting answers the handshake with HTTP 403.
            await send({"type": "websocket.close", "code": 4000 + e.status, "reason": e.detail})
            return
        body = json.dumps({"detail": e.detail}).encode()
        headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
        if e.status == 401:
            headers.append((b"www-authenticate", b"Bearer"))
        await send({"type": "http.response.start", "status": e.status, "headers": headers})
        await send({"type": "http.response.body", "body": body})


def principal_of(conn) -> Principal:
    """The authenticated caller of a request or WebSocket."""
    principal = conn.scope.get("state", {}).get("principal")
    if principal is None:  # a public path, or the middleware isn't installed: fail closed
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Not authenticated")
    return principal

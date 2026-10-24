"""Credentials in network policies: encrypted at rest, write-only through the API.

A network policy carries the header values the credential proxy injects
(API keys). The state store keeps policies sealed with a key that lives
beside it in ``STATE_DIR`` (or ``FLINT_SECRET_KEY``), so a copied database
or backup holds no usable credential. The API only ever returns policies
redacted; a client that reads a policy, edits it and writes it back sends
the redacted values unchanged, and those keep the stored credential.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import threading

from cryptography.fernet import Fernet, InvalidToken

from .config import STATE_DIR

KEY_PATH = os.path.join(STATE_DIR, "secret.key")
_PREFIX = "fernet:"

_fernet: Fernet | None = None
_lock = threading.Lock()


def _load_key() -> bytes:
    env = os.environ.get("FLINT_SECRET_KEY", "").strip()
    if env:
        return env.encode()
    try:
        with open(KEY_PATH, "rb") as f:
            return f.read().strip()
    except FileNotFoundError:
        pass
    key = Fernet.generate_key()
    os.makedirs(os.path.dirname(KEY_PATH), exist_ok=True)
    try:
        fd = os.open(KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:  # another process created it first
        with open(KEY_PATH, "rb") as f:
            return f.read().strip()
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    return key


def _cipher() -> Fernet:
    global _fernet
    with _lock:
        if _fernet is None:
            key = _load_key()
            try:
                _fernet = Fernet(key)
            except ValueError:
                # Not a Fernet key: derive one, so any passphrase works.
                _fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(key).digest()))
        return _fernet


def derived_key(label: str) -> str:
    """A key for another purpose (e.g. signing cookies), derived from the secret key."""
    return hashlib.sha256(_load_key() + b"\0" + label.encode()).hexdigest()


def seal(text: str) -> str:
    return _PREFIX + _cipher().encrypt(text.encode()).decode()


def is_sealed(stored: str) -> bool:
    return stored.startswith(_PREFIX)


def unseal(stored: str) -> str:
    """The plaintext of a sealed value. Values stored before sealing pass through."""
    if not is_sealed(stored):
        return stored
    try:
        return _cipher().decrypt(stored[len(_PREFIX):].encode()).decode()
    except InvalidToken as e:
        raise RuntimeError(f"Can't decrypt a stored secret: the key in {KEY_PATH} (or FLINT_SECRET_KEY) "
                           "isn't the one it was sealed with") from e


# ── Redaction ────────────────────────────────────────────────────────────

_REDACT_RE = re.compile(r"^(.{4}).+(.{4})$", re.DOTALL)


def redact(value: str) -> str:
    """Enough of a credential to recognise it, not to use it."""
    m = _REDACT_RE.match(value)
    return f"{m.group(1)}***{m.group(2)}" if m else "***"


def _map_headers(policy: dict | None, fn) -> dict | None:
    """A copy of ``policy`` with fn(domain, name, value) for every transform header value."""
    if policy is None:
        return None
    allow = policy.get("allow")
    if not isinstance(allow, dict):
        return policy
    out: dict = {}
    for domain, rules in allow.items():
        new_rules = []
        for rule in rules if isinstance(rules, list) else []:
            transforms = rule.get("transform") if isinstance(rule, dict) else None
            if not transforms:
                new_rules.append(rule)
                continue
            new_transforms = []
            for t in transforms:
                headers = t.get("headers") if isinstance(t, dict) else None
                if not headers:
                    new_transforms.append(t)
                    continue
                new_transforms.append({**t, "headers": {k: fn(domain, k, v) for k, v in headers.items()}})
            new_rules.append({**rule, "transform": new_transforms})
        out[domain] = new_rules
    return {**policy, "allow": out}


def redact_policy(policy: dict | None) -> dict | None:
    """A copy of the network policy with every credential value redacted."""
    return _map_headers(policy, lambda _d, _k, v: redact(v) if isinstance(v, str) else "***")


def header_values(policy: dict | None) -> dict[str, dict[str, str]]:
    """domain -> header -> value, as the proxy applies them (later transforms win)."""
    values: dict[str, dict[str, str]] = {}
    for domain, rules in ((policy or {}).get("allow") or {}).items():
        for rule in rules:
            for t in rule.get("transform") or []:
                values.setdefault(domain, {}).update(t.get("headers") or {})
    return values


def keep_unchanged(new: dict, current: dict | None) -> dict:
    """``new`` with every value that is the redacted form of the current value
    of the same header on the same domain replaced by that current value.

    So a read-modify-write of a policy keeps the credentials it didn't change.
    """
    old = header_values(current)

    def keep(domain: str, name: str, value: str) -> str:
        prev = old.get(domain, {}).get(name)
        return prev if prev is not None and value == redact(prev) else value

    return _map_headers(new, keep)

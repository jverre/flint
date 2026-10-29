"""A sandbox's network policy: which domains get which credential headers.

Policies arrive as JSON (``{"allow": {domain: [{"transform": [{"headers": {...}}]}]}}``).
:meth:`NetworkPolicy.parse` validates one once, where it enters (the API or
the runtime boundary); everything past that works with the typed value. The
original JSON is kept as given, so a policy reads back exactly as it was set.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any

from . import _secrets


class PolicyError(ValueError):
    """The policy JSON is malformed; the message says where."""


@dataclass(frozen=True)
class NetworkPolicy:
    # domain -> header -> value, merged over the domain's transforms in order (later wins).
    headers: dict[str, dict[str, str]] = field(default_factory=dict)
    # Any rule has a transform, even one without headers: the proxy then runs.
    has_transforms: bool = False
    _json: dict = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def parse(cls, value: object) -> NetworkPolicy:
        if isinstance(value, NetworkPolicy):
            return value
        if not isinstance(value, dict):
            raise PolicyError("Network policy must be a JSON object")
        allow = value.get("allow")
        headers: dict[str, dict[str, str]] = {}
        has_transforms = False
        if allow is not None:
            if not isinstance(allow, dict):
                raise PolicyError("'allow' must be a JSON object mapping domains to rule lists")
            for domain, rules in allow.items():
                if not isinstance(domain, str):
                    raise PolicyError(f"Domain key must be a string, got {type(domain).__name__}")
                if not isinstance(rules, list):
                    raise PolicyError(f"Rules for domain '{domain}' must be a list")
                for rule in rules:
                    if not isinstance(rule, dict):
                        raise PolicyError(f"Each rule for domain '{domain}' must be a JSON object")
                    transforms = rule.get("transform") or []
                    has_transforms = has_transforms or bool(transforms)
                    if not isinstance(transforms, list):
                        raise PolicyError(f"Each transform for domain '{domain}' must be a JSON object")
                    for transform in transforms:
                        if not isinstance(transform, dict):
                            raise PolicyError(f"Each transform for domain '{domain}' must be a JSON object")
                        values = transform.get("headers")
                        if values is None:
                            continue
                        if not isinstance(values, dict):
                            raise PolicyError(f"Transform headers for domain '{domain}' must be a JSON object")
                        for k, v in values.items():
                            if not isinstance(k, str) or not isinstance(v, str):
                                raise PolicyError(f"Header names and values must be strings (domain '{domain}')")
                        if values:
                            headers.setdefault(domain, {}).update(values)
        return cls(headers, has_transforms, copy.deepcopy(value))

    @classmethod
    def optional(cls, value: object) -> NetworkPolicy | None:
        return None if value is None else cls.parse(value)

    @classmethod
    def from_json_text(cls, text: str | None) -> NetworkPolicy | None:
        """A policy as the state store keeps it, or None if there is none (or it's unreadable)."""
        if not text:
            return None
        try:
            return cls.parse(json.loads(text))
        except (ValueError, TypeError):
            return None

    def to_json(self) -> dict:
        """The policy as it was given, credentials included."""
        return copy.deepcopy(self._json)

    def to_json_text(self) -> str:
        return json.dumps(self._json)

    def redacted(self) -> dict:
        """The policy as the API shows it: every credential value redacted."""
        return _secrets.redact_policy(self.to_json())

    def keeping_credentials_of(self, current: NetworkPolicy | None) -> NetworkPolicy:
        """This policy, with header values sent back in redacted form replaced by ``current``'s values.

        So a client can read-modify-write a policy whose credentials it can't read.
        """
        return NetworkPolicy.parse(_secrets.keep_unchanged(self.to_json(), current.to_json() if current else None))


def redacted(policy: NetworkPolicy | None) -> dict | None:
    return policy.redacted() if policy is not None else None


def as_json(policy: Any) -> Any:
    """``policy`` as JSON-serialisable data (for crossing a process boundary)."""
    return policy.to_json() if isinstance(policy, NetworkPolicy) else policy

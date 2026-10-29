"""Errors the SDK raises when the daemon (or a cluster's control plane) refuses a request.

Catch :class:`FlintError` for any of them, or a subclass for one kind::

    from flint import Sandbox, NotFound

    try:
        Sandbox.connect(vm_id).resume()
    except NotFound:
        ...

An :class:`APIError` is also an ``httpx.HTTPStatusError`` (``.response``
holds the answer) and a ``RuntimeError``, which the SDK raised before.
"""

from __future__ import annotations

import httpx


class FlintError(Exception):
    """Base class of the SDK's errors."""


class APIError(FlintError, httpx.HTTPStatusError, RuntimeError):
    """The API answered with an error. ``status_code`` and ``detail`` say which."""

    def __init__(self, response: httpx.Response) -> None:
        self.status_code = response.status_code
        self.detail = detail_of(response)
        super().__init__(f"{self.status_code}: {self.detail}", request=response.request, response=response)


class InvalidRequest(APIError):
    """400: the request is malformed."""


class Unauthorized(APIError):
    """401: no valid API token."""


class Forbidden(APIError):
    """403: the token may not do this (e.g. act for another tenant)."""


class NotFound(APIError):
    """404: no such sandbox, template or volume (or not one the token can see)."""


class Conflict(APIError):
    """409: the resource's state doesn't allow it (e.g. resuming a running sandbox)."""


class LimitExceeded(APIError):
    """429: a quota or limit is reached; ``Retry-After`` may say when to try again."""


class Unavailable(APIError):
    """5xx the request may succeed later (not ready, busy, no capacity) or that isn't supported here (501)."""


_BY_STATUS = {400: InvalidRequest, 401: Unauthorized, 403: Forbidden, 404: NotFound, 409: Conflict,
              422: InvalidRequest, 429: LimitExceeded}


def detail_of(response: httpx.Response) -> str:
    """The error's explanation: the JSON ``detail`` the API sends, else the body."""
    try:
        detail = response.json().get("detail", response.text)
    except (ValueError, AttributeError):
        detail = response.text
    return detail if isinstance(detail, str) else str(detail)


def error_for(response: httpx.Response) -> APIError:
    status = response.status_code
    cls = _BY_STATUS.get(status) or (Unavailable if status >= 500 and status != 500 else APIError)
    return cls(response)


def raise_for_status(response: httpx.Response) -> None:
    """Raise the :class:`APIError` for ``response`` if it's an error."""
    if not response.is_success:
        raise error_for(response)

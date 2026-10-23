"""How clients reach the daemon: its Unix socket (``unix:///path``) or HTTP(S)."""

from __future__ import annotations

import httpx
import websockets.sync.client as ws_sync

_UNIX = "unix://"
# Host name used in URLs sent over the socket; the daemon ignores it.
_SOCKET_HOST = "flint"


def socket_path(url: str) -> str | None:
    return url[len(_UNIX):] if url.startswith(_UNIX) else None


def http_base(url: str) -> str:
    """The base URL httpx requests use (over the socket, a placeholder host)."""
    return f"http://{_SOCKET_HOST}" if socket_path(url) else url


def http_client(url: str, **kwargs) -> httpx.Client:
    path = socket_path(url)
    if path:
        return httpx.Client(transport=httpx.HTTPTransport(uds=path), base_url=http_base(url), **kwargs)
    return httpx.Client(base_url=url, **kwargs)


def ws_connect(url: str, path: str, headers: dict[str, str] | None = None, **kwargs):
    """Open a WebSocket to ``path`` (e.g. ``/events``) on the daemon at ``url``."""
    sock = socket_path(url)
    if sock:
        return ws_sync.unix_connect(sock, f"ws://{_SOCKET_HOST}{path}", additional_headers=headers or None, **kwargs)
    base = url.replace("http://", "ws://").replace("https://", "wss://")
    return ws_sync.connect(base + path, additional_headers=headers or None, **kwargs)

"""HTTP requests to a sandbox's guest agent.

The guest runs tenant code as root, so it can replace the agent with anything.
Nothing it does may cost the host more than a bounded amount:

- Connections start non-blocking, so whoever opens one (the privileged
  runtime) never waits on a guest that drops the SYN.
- The runtime's own requests are bounded in size and in total time: a
  per-read socket timeout alone lets a guest trickle a byte a minute and
  hold a thread forever.
- The API reads replies asynchronously and streams them through, so a
  stalled guest holds only a coroutine and a socket, and a large reply only
  a chunk at a time.
"""

from __future__ import annotations

import asyncio
import errno
import http.client
import os
import select
import socket
import threading
import time
from collections.abc import AsyncIterable, AsyncIterator

import h11

# The runtime's own requests (volume mounts, CA install) carry small JSON.
MAX_RESPONSE = 128 << 20
_CHUNK = 64 << 10


class GuestAgentError(RuntimeError):
    """The guest agent's reply was refused. ``status`` is the HTTP status for API callers."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status, self.detail = status, detail


def _timed_out(timeout: float) -> GuestAgentError:
    return GuestAgentError(504, f"Guest agent did not reply within {timeout:g}s")


def connect(agent_url: str) -> socket.socket:
    """Start connecting to the agent at ``agent_url`` (``http://host:port``), in the caller's netns.

    Returns at once with the connection in progress; the request functions
    below wait for it under their own deadline.
    """
    host, _, port = agent_url.removeprefix("http://").rstrip("/").rpartition(":")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setblocking(False)
    code = sock.connect_ex((host, int(port)))
    if code not in (0, errno.EINPROGRESS):
        sock.close()
        raise GuestAgentError(502, f"Guest agent unreachable: {os.strerror(code)}")
    return sock


def _connect_error(sock: socket.socket) -> GuestAgentError | None:
    code = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
    return GuestAgentError(502, f"Guest agent unreachable: {os.strerror(code)}") if code else None


def request(sock: socket.socket, method: str, path: str, body: bytes | None = None,
            timeout: float = 65, limit: int = MAX_RESPONSE) -> tuple[int, bytes]:
    """One blocking request over ``sock``, bounded in size and total time; ``sock`` is closed after."""
    deadline = time.monotonic() + timeout
    expired = threading.Event()

    def expire() -> None:
        expired.set()
        try:
            sock.shutdown(socket.SHUT_RDWR)  # wakes a blocked read
        except OSError:
            pass

    timer = threading.Timer(timeout, expire)
    timer.daemon = True
    conn = http.client.HTTPConnection("guest", timeout=timeout)
    conn.sock = sock
    try:
        _, writable, _ = select.select([], [sock], [], timeout)
        if not writable:
            raise _timed_out(timeout)
        if error := _connect_error(sock):
            raise error
        sock.settimeout(max(deadline - time.monotonic(), 0.001))
        timer.start()
        headers = {"Content-Type": "application/json"} if body is not None else {}
        conn.request(method, path, body, headers)
        response = conn.getresponse()
        if response.length is not None and response.length > limit:
            raise GuestAgentError(502, f"Guest agent reply exceeds {limit >> 20} MiB")
        # read(n) returns what arrived before the connection closed, so a
        # reply cut short (by the deadline or the guest) must be checked for.
        data = response.read(limit + 1)
        if expired.is_set():
            raise _timed_out(timeout)
        if len(data) > limit:
            raise GuestAgentError(502, f"Guest agent reply exceeds {limit >> 20} MiB")
        if response.length:
            raise GuestAgentError(502, "Guest agent reply was cut short")
        return response.status, data
    except (OSError, http.client.HTTPException) as e:
        if expired.is_set():
            raise _timed_out(timeout) from None
        raise GuestAgentError(502, f"Guest agent request failed: {e}") from e
    finally:
        timer.cancel()
        conn.close()


async def _next_event(sock: socket.socket, conn: h11.Connection):
    loop = asyncio.get_running_loop()
    try:
        while (event := conn.next_event()) is h11.NEED_DATA:
            conn.receive_data(await loop.sock_recv(sock, _CHUNK))
        return event
    except (h11.ProtocolError, OSError) as e:
        raise GuestAgentError(502, f"Guest agent reply is invalid: {e}") from e


class Reply:
    """A guest agent's reply, whose body is read as it arrives."""

    def __init__(self, sock: socket.socket, conn: h11.Connection, response: h11.Response, timeout: float) -> None:
        self._sock, self._conn, self._timeout = sock, conn, timeout
        self.status = response.status_code
        self.headers = {name.decode().lower(): value.decode("latin-1") for name, value in response.headers}

    async def chunk(self) -> bytes | None:
        """The next piece of the body, or None at its end. Raises 504 if the guest stalls."""
        try:
            async with asyncio.timeout(self._timeout):
                while True:
                    event = await _next_event(self._sock, self._conn)
                    if isinstance(event, h11.Data):
                        return bytes(event.data)
                    if isinstance(event, h11.EndOfMessage):
                        return None
                    if isinstance(event, h11.ConnectionClosed):
                        raise GuestAgentError(502, "Guest agent reply was cut short")
        except TimeoutError:
            raise _timed_out(self._timeout) from None

    async def head(self, limit: int) -> tuple[bytes, bool]:
        """Up to about ``limit`` bytes of the body, within the timeout, and whether that is all of it.

        Closes the connection on failure or when the body is complete.
        """
        parts, size = [], 0
        try:
            async with asyncio.timeout(self._timeout):
                while size <= limit:
                    data = await self.chunk()
                    if data is None:
                        self.close()
                        return b"".join(parts), True
                    parts.append(data)
                    size += len(data)
        except TimeoutError:
            self.close()
            raise _timed_out(self._timeout) from None
        except BaseException:
            self.close()
            raise
        return b"".join(parts), False

    async def rest(self) -> AsyncIterator[bytes]:
        """The remaining body, a chunk at a time; closes the connection when done."""
        try:
            while (data := await self.chunk()) is not None:
                yield data
        finally:
            self.close()

    def close(self) -> None:
        self._sock.close()


async def _connected(sock: socket.socket) -> None:
    loop = asyncio.get_running_loop()
    ready = loop.create_future()
    loop.add_writer(sock.fileno(), lambda: ready.done() or ready.set_result(None))
    try:
        await ready
    finally:
        loop.remove_writer(sock.fileno())
    if error := _connect_error(sock):
        raise error


async def wait_connected(sock: socket.socket, timeout: float = 65) -> None:
    """Wait until a socket from :func:`connect` is connected."""
    sock.setblocking(False)
    try:
        async with asyncio.timeout(timeout):
            await _connected(sock)
    except TimeoutError:
        raise _timed_out(timeout) from None


async def open(sock: socket.socket, method: str, path: str,
               body: bytes | AsyncIterable[bytes] | None = None, timeout: float = 65) -> Reply:
    """Send a request over ``sock`` and return the reply once its headers arrive.

    Connecting, sending the request headers, and waiting for the reply
    headers each must finish within ``timeout``; a streamed ``body`` may take
    as long as it needs, as long as each chunk goes out within ``timeout``.
    """
    loop = asyncio.get_running_loop()
    conn = h11.Connection(h11.CLIENT)

    async def send(*events) -> None:
        data = b"".join(conn.send(event) or b"" for event in events)
        async with asyncio.timeout(timeout):
            await loop.sock_sendall(sock, data)

    headers = [("Host", "guest")]
    if isinstance(body, bytes):
        headers += [("Content-Type", "application/json"), ("Content-Length", str(len(body)))]
    elif body is not None:
        headers += [("Content-Type", "application/octet-stream"), ("Transfer-Encoding", "chunked")]
    try:
        await wait_connected(sock, timeout)
        if isinstance(body, bytes):
            await send(h11.Request(method=method, target=path, headers=headers), h11.Data(data=body), h11.EndOfMessage())
        else:
            await send(h11.Request(method=method, target=path, headers=headers))
            if body is not None:
                async for data in body:
                    if data:
                        await send(h11.Data(data=data))
            await send(h11.EndOfMessage())
        reply = None
        async with asyncio.timeout(timeout):
            while not isinstance(reply, h11.Response):
                reply = await _next_event(sock, conn)
                if isinstance(reply, h11.ConnectionClosed):
                    raise GuestAgentError(502, "Guest agent closed the connection without replying")
        return Reply(sock, conn, reply, timeout)
    except TimeoutError:
        sock.close()
        raise _timed_out(timeout) from None
    except (h11.ProtocolError, OSError) as e:
        sock.close()
        raise GuestAgentError(502, f"Guest agent request failed: {e}") from e
    except BaseException:
        sock.close()
        raise


async def call(sock: socket.socket, method: str, path: str, body: bytes | None = None,
               timeout: float = 65, limit: int = 1 << 20) -> tuple[int, bytes]:
    """A small request: the whole reply, which must fit in ``limit``, within ``timeout``."""
    reply = await open(sock, method, path, body, timeout)
    data, complete = await reply.head(limit)
    reply.close()
    if not complete:
        raise GuestAgentError(502, f"Guest agent reply exceeds {limit >> 10} KiB")
    return reply.status, data

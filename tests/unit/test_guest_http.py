"""Whatever a guest agent sends, it costs the host a bounded amount of time and memory."""

from __future__ import annotations

import asyncio
import socket
import threading
import time

import h11
import pytest

from flint.core.guest import http as _guest_http


def _guest(send) -> socket.socket:
    """The host end of a connection whose guest end runs ``send`` once the request arrives."""
    host, guest = socket.socketpair()

    def serve():
        with guest:
            guest.recv(65536)
            try:
                send(guest)
            except OSError:
                pass  # the host hung up

    threading.Thread(target=serve, daemon=True).start()
    return host


def _trickle(g):
    g.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n")
    for _ in range(1000):  # well inside any per-read timeout each time
        g.sendall(b"x")
        time.sleep(0.1)


def _reply(body: bytes):
    return lambda g: g.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body))


# ── The runtime's own blocking requests ──────────────────────────────────────


def test_reply_round_trips():
    assert _guest_http.request(_guest(_reply(b"ok")), "GET", "/health", timeout=5) == (200, b"ok")


def test_trickling_guest_is_cut_off_at_the_deadline():
    t0 = time.monotonic()
    with pytest.raises(_guest_http.GuestAgentError) as e:
        _guest_http.request(_guest(_trickle), "GET", "/files", timeout=1)
    assert e.value.status == 504
    assert time.monotonic() - t0 < 3, "the deadline covers the whole reply, not each read"


def test_endless_reply_is_refused():
    def flood(g):
        g.sendall(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
        chunk = b"%x\r\n%s\r\n" % (65536, b"x" * 65536)
        while True:
            g.sendall(chunk)

    with pytest.raises(_guest_http.GuestAgentError) as e:
        _guest_http.request(_guest(flood), "POST", "/exec", b"{}", timeout=10, limit=1 << 20)
    assert e.value.status == 502 and "exceeds" in e.value.detail


# ── The API's streamed requests ──────────────────────────────────────────────


def test_large_reply_streams_a_chunk_at_a_time():
    body = bytes(range(256)) * (80 << 12)  # 20 MiB

    async def main():
        reply = await _guest_http.open(_guest(_reply(body)), "GET", "/files", timeout=5)
        head, complete = await reply.head(1 << 20)
        assert not complete and len(head) < 2 << 20
        rest = [chunk async for chunk in reply.rest()]
        assert max(map(len, rest)) <= 64 << 10
        assert head + b"".join(rest) == body

    asyncio.run(main())


def test_trickling_guest_is_cut_off_before_its_reply_is_held():
    async def main():
        reply = await _guest_http.open(_guest(_trickle), "GET", "/files", timeout=1)
        await reply.head(1 << 20)

    t0 = time.monotonic()
    with pytest.raises(_guest_http.GuestAgentError) as e:
        asyncio.run(main())
    assert e.value.status == 504 and time.monotonic() - t0 < 3


def test_stalled_stream_is_cut_off():
    def stall(g):
        g.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % (4 << 20) + b"x" * (2 << 20))
        time.sleep(30)

    async def main():
        reply = await _guest_http.open(_guest(stall), "GET", "/files", timeout=1)
        _, complete = await reply.head(1 << 20)
        assert not complete
        async for _ in reply.rest():
            pass

    with pytest.raises(_guest_http.GuestAgentError) as e:
        asyncio.run(main())
    assert e.value.status == 504


def test_streamed_upload_reaches_the_guest():
    received = []

    def receive(g):
        conn = h11.Connection(h11.SERVER)
        while True:
            event = conn.next_event()
            if event is h11.NEED_DATA:
                conn.receive_data(g.recv(65536))
            elif isinstance(event, h11.Data):
                received.append(bytes(event.data))
            elif isinstance(event, h11.EndOfMessage):
                break
        g.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 11\r\n\r\n{\"ok\":true}")

    host, guest = socket.socketpair()
    threading.Thread(target=lambda: receive(guest), daemon=True).start()

    async def chunks():
        for _ in range(64):
            yield b"y" * (256 << 10)

    async def main():
        reply = await _guest_http.open(host, "POST", "/files?path=/x", chunks(), timeout=5)
        return reply.status, (await reply.head(1024))[0]

    assert asyncio.run(main()) == (200, b'{"ok":true}')
    assert len(b"".join(received)) == 16 << 20


def test_unreachable_agent_is_a_bad_gateway():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]  # nothing listens here once closed

    async def main():
        await _guest_http.call(_guest_http.connect(f"http://127.0.0.1:{port}"), "GET", "/health", timeout=5)

    with pytest.raises(_guest_http.GuestAgentError) as e:
        asyncio.run(main())
    assert e.value.status == 502

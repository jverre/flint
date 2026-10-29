"""Bounded JSON RPC over a private Unix socket; never pickle or dynamic dispatch."""
from __future__ import annotations

import array
import json
import socket
import struct
import time

MAX_FRAME = 4 << 20
# Responses come from the trusted runtime and may carry long event pages.
MAX_RESPONSE = 64 << 20


def receive(sock: socket.socket, limit: int = MAX_FRAME) -> tuple[dict, int | None]:
    header, ancillary, flags, _ = sock.recvmsg(
        4, socket.CMSG_SPACE(4), socket.MSG_WAITALL | getattr(socket, "MSG_CMSG_CLOEXEC", 0)
    )
    received = []
    for level, kind, data in ancillary:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            fds = array.array("i")
            fds.frombytes(data[:len(data) - len(data) % fds.itemsize])
            received.extend(fds)
    try:
        if len(header) != 4 or flags & socket.MSG_CTRUNC or len(received) > 1:
            raise ValueError("Invalid runtime frame")
        length, = struct.unpack("!I", header)
        if not 0 < length <= limit:
            raise ValueError("Runtime frame exceeds limit")
        payload = bytearray()
        while len(payload) < length:
            chunk = sock.recv(min(length - len(payload), 65536))
            if not chunk:
                raise ValueError("Truncated runtime frame")
            payload.extend(chunk)
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise ValueError("Runtime frame must be an object")
        return value, received[0] if received else None
    except BaseException:
        import os
        for fd in received:
            os.close(fd)
        raise


def send(sock: socket.socket, value: dict, fd: int | None = None, limit: int = MAX_FRAME) -> None:
    payload = json.dumps(value, allow_nan=False).encode()
    if len(payload) > limit:
        raise ValueError("Runtime frame exceeds limit")
    ancillary = [] if fd is None else [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [fd]))]
    header = struct.pack("!I", len(payload))
    sent = sock.sendmsg([header], ancillary)
    sock.sendall(header[sent:] + payload)


class RuntimeClient:
    def __init__(self, path: str) -> None:
        self.path = path

    def request(self, operation: str, **arguments):
        from .eventlog import actor
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(180)
            # A full accept backlog fails a Unix connect with EAGAIN instead of waiting.
            deadline = time.monotonic() + 10
            while True:
                try:
                    conn.connect(self.path)
                    break
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(0.01)
            send(conn, {"operation": operation, "arguments": arguments, "actor": list(actor.get())})
            result, fd = receive(conn, MAX_RESPONSE)
        if "error" in result:
            if fd is not None:
                import os
                os.close(fd)
            # The API's exception handler answers with the error's status.
            from flint.core.errors import from_status
            error = result["error"]
            raise from_status(error.get("status"), str(error.get("detail", "")), error.get("headers"))
        return socket.socket(fileno=fd) if fd is not None else result["value"]

"""One-use mutually authenticated TLS relays for CH's UNIX migration sockets.

Works with pinned CH versions that predate native TLS migration. The VMM sees
only a jail-local socket; migration bytes cross hosts only inside mutual TLS.
"""

from __future__ import annotations

import os
import socket
import ssl
import struct
import threading
import time

from flint.core import config


def context(server: bool) -> ssl.SSLContext:
    identity = config.CLUSTER_IDENTITY_DIR
    ctx = ssl.create_default_context(
        ssl.Purpose.CLIENT_AUTH if server else ssl.Purpose.SERVER_AUTH,
        cafile=f"{identity}/hosts-ca.pem",
    )
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(f"{identity}/host.crt", f"{identity}/host.key")
    ctx.verify_mode = ssl.CERT_REQUIRED
    if not server:
        # Host identities are UUID CNs; endpoint IPs need not be certificate SANs.
        # Verify the expected UUID explicitly after the CA check.
        ctx.check_hostname = False
    return ctx


def verify_peer(conn: ssl.SSLSocket, expected: str) -> None:
    names = [
        value
        for group in conn.getpeercert().get("subject", ())
        for key, value in group
        if key == "commonName"
    ]
    if names != [expected]:
        raise ssl.SSLError("migration peer is not the expected host")


def relay(left: socket.socket, right: socket.socket) -> None:
    # TCP half-close cannot be forwarded with SSLSocket.shutdown(SHUT_WR):
    # that truncates TLS records and discards the reverse-direction response.
    # Frame each direction independently; a zero frame carries its EOF.
    secure = left if isinstance(left, ssl.SSLSocket) else right
    local = right if secure is left else left
    errors = []

    def read_exact(size: int) -> bytes:
        result = bytearray()
        while len(result) < size:
            data = secure.recv(size - len(result))
            if not data:
                raise ConnectionResetError("truncated TLS migration frame")
            result.extend(data)
        return bytes(result)

    def send():
        try:
            while data := local.recv(65536):
                secure.sendall(struct.pack("!I", len(data)) + data)
            secure.sendall(bytes(4))
        except OSError as exc:
            errors.append(exc)
            # Abort both readers if the local VMM or encrypted stream failed.
            for conn in (secure, local):
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    thread = threading.Thread(target=send, daemon=True)
    thread.start()
    try:
        while True:
            size = struct.unpack("!I", read_exact(4))[0]
            if size == 0:
                local.shutdown(socket.SHUT_WR)
                break
            if size > 65536:
                raise ValueError("oversized TLS migration frame")
            local.sendall(read_exact(size))
        thread.join(timeout=5)
        if thread.is_alive():
            raise TimeoutError("migration VMM did not finish its stream")
        if errors:
            raise errors[0]
    finally:
        if thread.is_alive():
            for conn in (secure, local):
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            thread.join(timeout=1)


class MigrationRelay:
    def __init__(self) -> None:
        self.listener: socket.socket | None = None
        self.sockets: list[socket.socket] = []
        self.error: str = ""
        self.done = threading.Event()
        self._closed = threading.Event()

    def close(self) -> None:
        self._closed.set()
        for conn in [self.listener, *self.sockets]:
            if conn:
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                conn.close()

    def receiver(self, local_ip: str, unix_path: str, source_host: str) -> int:
        self.listener = socket.create_server((local_ip, 0))
        self.listener.settimeout(120)
        port = self.listener.getsockname()[1]

        def serve():
            try:
                raw, _ = self.listener.accept()
                raw.settimeout(900)
                self.sockets.append(raw)
                conn = context(True).wrap_socket(raw, server_side=True)
                self.sockets.append(conn)
                verify_peer(conn, source_host)
                local = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                local.settimeout(900)
                self.sockets.append(local)
                for _ in range(100):
                    if self._closed.is_set():
                        return
                    try:
                        local.connect(unix_path)
                        break
                    except (FileNotFoundError, ConnectionRefusedError):
                        time.sleep(0.1)
                else:
                    raise TimeoutError("VMM migration receiver not ready")
                relay(conn, local)
            except Exception as exc:
                self.error = str(exc)
            finally:
                self.done.set()
                self.close()

        threading.Thread(
            target=serve, daemon=True, name="migration-receiver-tls"
        ).start()
        return port

    def sender(self, unix_path: str, address: str, port: int, target_host: str) -> None:
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(unix_path)
        os.chown(unix_path, config.CH_UID, config.CH_UID)
        os.chmod(unix_path, 0o600)
        self.listener.listen(1)
        self.listener.settimeout(30)

        def serve():
            try:
                local, _ = self.listener.accept()
                local.settimeout(900)
                self.sockets.append(local)
                raw = socket.create_connection((address, port), timeout=10)
                raw.settimeout(900)
                self.sockets.append(raw)
                conn = context(False).wrap_socket(raw, server_hostname=target_host)
                self.sockets.append(conn)
                verify_peer(conn, target_host)
                relay(local, conn)
            except Exception as exc:
                self.error = str(exc)
            finally:
                self.done.set()
                self.close()

        threading.Thread(target=serve, daemon=True, name="migration-sender-tls").start()

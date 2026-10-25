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


_FRAME = 65536  # largest payload per frame
_IDLE_TIMEOUT = 900.0  # seconds without progress in either direction


def relay(left: socket.socket, right: socket.socket) -> None:
    """Copy bytes both ways between the TLS socket and the VMM's local one.

    TCP half-close cannot be forwarded with SSLSocket.shutdown(SHUT_WR):
    that truncates TLS records and discards the reverse-direction response.
    So each direction is framed (a 4-byte length, then the bytes), and a
    zero-length frame carries its EOF.

    One thread does everything: an SSL connection must never be read and
    written from two threads at once (OpenSSL corrupts the stream, "bad
    record mac"). Both sockets are non-blocking; the loop waits on whichever
    can make progress.
    """
    import selectors

    secure = left if isinstance(left, ssl.SSLSocket) else right
    local = right if secure is left else left
    secure.setblocking(False)
    local.setblocking(False)

    to_secure = bytearray()  # frames waiting to be encrypted and sent
    to_local = bytearray()  # payload waiting for the VMM
    frames = bytearray()  # decrypted bytes not yet parsed into frames
    local_eof = remote_eof = local_closed = False
    want_write_to_read = False  # an SSL write needs the peer's data first

    sel = selectors.DefaultSelector()
    try:
        while True:
            progressed = False

            # VMM -> peer: frame what the VMM sent; its EOF becomes a zero frame.
            if not local_eof and len(to_secure) < 4 * _FRAME:
                try:
                    data = local.recv(_FRAME)
                except BlockingIOError:
                    pass
                else:
                    to_secure += struct.pack("!I", len(data)) + data if data else bytes(4)
                    local_eof = not data
                    progressed = True

            # Peer -> VMM: decrypt what's there (including what OpenSSL buffered).
            if not remote_eof and len(to_local) < 4 * _FRAME:
                try:
                    data = secure.recv(_FRAME)
                except (ssl.SSLWantReadError, BlockingIOError):
                    pass
                except ssl.SSLWantWriteError:
                    pass
                else:
                    if not data:
                        raise ConnectionResetError("truncated TLS migration frame")
                    frames += data
                    progressed = True
                    while len(frames) >= 4:
                        size = struct.unpack("!I", frames[:4])[0]
                        if size > _FRAME:
                            raise ValueError("oversized TLS migration frame")
                        if size == 0:
                            remote_eof = True
                            del frames[:4]
                            break
                        if len(frames) < 4 + size:
                            break
                        to_local += frames[4:4 + size]
                        del frames[:4 + size]

            # Send what's waiting, each way.
            want_write_to_read = False
            if to_secure:
                try:
                    # Same leading bytes on a retry, as OpenSSL requires.
                    sent = secure.send(to_secure[:16384])
                except ssl.SSLWantReadError:
                    want_write_to_read = True
                except (ssl.SSLWantWriteError, BlockingIOError):
                    pass
                else:
                    del to_secure[:sent]
                    progressed = progressed or sent > 0
            if to_local:
                try:
                    sent = local.send(to_local)
                except BlockingIOError:
                    pass
                else:
                    del to_local[:sent]
                    progressed = progressed or sent > 0
            if remote_eof and not to_local and not local_closed:
                local.shutdown(socket.SHUT_WR)
                local_closed = True
                progressed = True

            if local_eof and not to_secure and remote_eof and local_closed:
                return
            if progressed or secure.pending():
                continue

            # Nothing to do now: wait for a socket that can move bytes.
            interest = {local: 0, secure: 0}
            if not local_eof and len(to_secure) < 4 * _FRAME:
                interest[local] |= selectors.EVENT_READ
            if to_local:
                interest[local] |= selectors.EVENT_WRITE
            if (not remote_eof and len(to_local) < 4 * _FRAME) or want_write_to_read:
                interest[secure] |= selectors.EVENT_READ
            if to_secure and not want_write_to_read:
                interest[secure] |= selectors.EVENT_WRITE
            for sock, events in interest.items():
                if events:
                    sel.register(sock, events)
            try:
                if not sel.select(_IDLE_TIMEOUT):
                    raise TimeoutError("migration stream made no progress for 15 minutes")
            finally:
                for sock in list(sel.get_map().values()):
                    sel.unregister(sock.fileobj)
    finally:
        sel.close()


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
        # This socket is inside the source VMM's private jail.
        owner = os.stat(os.path.dirname(unix_path))
        os.chown(unix_path, owner.st_uid, owner.st_gid, follow_symlinks=False)
        os.chmod(unix_path, 0o600, follow_symlinks=False)
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

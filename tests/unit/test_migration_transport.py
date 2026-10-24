"""Exercise the actual TLS relay with one-use UNIX endpoints."""

import os
import shutil
import socket
import ssl
import subprocess
import threading

import pytest

from flint.cluster import migration_transport as transport


def test_migration_bytes_cross_mutually_authenticated_tls(tmp_path, monkeypatch):
    if not shutil.which("openssl"):
        pytest.skip("openssl required to mint test identities")

    def openssl(*args):
        subprocess.run(
            ["openssl", *args],
            cwd=tmp_path,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

    openssl(
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        "ca.key",
        "-out",
        "ca.pem",
        "-days",
        "1",
        "-subj",
        "/CN=test-ca",
    )
    for host in ("source", "target"):
        openssl(
            "req",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            host + ".key",
            "-out",
            host + ".csr",
            "-subj",
            "/CN=" + host,
        )
        openssl(
            "x509",
            "-req",
            "-in",
            host + ".csr",
            "-CA",
            "ca.pem",
            "-CAkey",
            "ca.key",
            "-CAcreateserial",
            "-out",
            host + ".pem",
            "-days",
            "1",
        )

    def context(server):
        ctx = ssl.create_default_context(
            ssl.Purpose.CLIENT_AUTH if server else ssl.Purpose.SERVER_AUTH,
            cafile=str(tmp_path / "ca.pem"),
        )
        if not server:
            ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_REQUIRED
        host = "target" if server else "source"
        ctx.load_cert_chain(
            str(tmp_path / (host + ".pem")), str(tmp_path / (host + ".key"))
        )
        return ctx

    monkeypatch.setattr(transport, "context", context)
    monkeypatch.setattr(os, "chown", lambda *args: None)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    dest_path = str(tmp_path / "receive.sock")
    listener.bind(dest_path)
    listener.listen(1)
    payload = os.urandom(1024 * 1024)
    received = bytearray()
    errors = []

    def vmm_receive():
        try:
            conn, _ = listener.accept()
            with conn:
                conn.settimeout(5)
                while len(received) < len(payload):
                    data = conn.recv(65536)
                    if not data:
                        break
                    received.extend(data)
                conn.sendall(b"migration-ack")
                conn.shutdown(socket.SHUT_WR)
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=vmm_receive, daemon=True)
    worker.start()
    receiver = transport.MigrationRelay()
    sender = transport.MigrationRelay()
    try:
        port = receiver.receiver("127.0.0.1", dest_path, "source")
        source_path = str(tmp_path / "send.sock")
        sender.sender(source_path, "127.0.0.1", port, "target")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(10)
            client.connect(source_path)
            client.sendall(payload)
            client.shutdown(socket.SHUT_WR)
            reply = bytearray()
            while len(reply) < len(b"migration-ack"):
                data = client.recv(100)
                if not data:
                    break
                reply.extend(data)
            assert bytes(reply) == b"migration-ack", (sender.error, receiver.error)
        worker.join(timeout=5)
        assert received == payload
        assert not errors
    finally:
        sender.close()
        receiver.close()
        listener.close()

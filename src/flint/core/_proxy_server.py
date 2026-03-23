"""Credential injection forward proxy server.

Runs as a standalone process inside a VM's network namespace. Intercepts HTTP
and HTTPS traffic redirected via iptables and injects headers based on
domain-matching rules.

Usage:
    python _proxy_server.py --port 8080 --rules /path/to/rules.json \
        --ca-cert /path/to/ca.pem --ca-key /path/to/ca-key.pem
"""

from __future__ import annotations

import argparse
import asyncio
import fnmatch
import json
import os
import socket
import ssl
import struct
import sys
import time

# ── Rule matching ────────────────────────────────────────────────────────────


def load_rules(rules_path: str) -> dict:
    """Load domain->headers rules from a JSON file."""
    try:
        with open(rules_path) as f:
            return json.load(f)
    except Exception:
        return {}


def match_domain(host: str, rules: dict) -> dict[str, str] | None:
    """Match a hostname against rules. Returns headers dict or None.

    Tries exact match first, then wildcard patterns.
    """
    if host in rules:
        return rules[host]
    for pattern, headers in rules.items():
        if "*" in pattern and fnmatch.fnmatch(host, pattern):
            return headers
    return None


def inject_headers(raw_request: bytes, headers_to_inject: dict[str, str]) -> bytes:
    """Inject/overwrite headers in a raw HTTP request.

    Headers are matched case-insensitively. Injected headers overwrite any
    existing headers with the same name (security requirement).
    """
    # Split into head and body
    sep = b"\r\n\r\n"
    idx = raw_request.find(sep)
    if idx == -1:
        return raw_request

    head = raw_request[:idx]
    body = raw_request[idx:]  # includes the separator

    lines = head.split(b"\r\n")
    request_line = lines[0]
    existing_headers = lines[1:]

    # Build a case-insensitive map of headers to inject
    inject_lower = {k.lower(): (k, v) for k, v in headers_to_inject.items()}

    # Filter out existing headers that we want to overwrite
    new_headers = []
    for hline in existing_headers:
        if b":" in hline:
            hname = hline.split(b":", 1)[0].strip().decode("latin-1").lower()
            if hname in inject_lower:
                continue  # skip — will be replaced
        new_headers.append(hline)

    # Add injected headers
    for lower_name, (orig_name, value) in inject_lower.items():
        new_headers.append(f"{orig_name}: {value}".encode("latin-1"))

    result = request_line + b"\r\n" + b"\r\n".join(new_headers) + body
    return result


# ── TLS certificate generation ───────────────────────────────────────────────

_cert_cache: dict[str, tuple[str, str]] = {}


def _generate_leaf_cert(hostname: str, ca_cert_path: str, ca_key_path: str) -> tuple[str, str]:
    """Generate a leaf TLS certificate for a hostname, signed by the CA."""
    if hostname in _cert_cache:
        return _cert_cache[hostname]

    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    import datetime

    # Load CA
    with open(ca_cert_path, "rb") as f:
        ca_cert = x509.load_pem_x509_certificate(f.read())
    with open(ca_key_path, "rb") as f:
        ca_key = serialization.load_pem_private_key(f.read(), password=None)

    # Generate leaf key
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    now = datetime.datetime.now(datetime.timezone.utc)

    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(hostname)]),
            critical=False,
        )
    )
    leaf_cert = builder.sign(ca_key, hashes.SHA256())

    # Write to temp files
    import tempfile
    cert_fd, cert_path = tempfile.mkstemp(suffix=".pem")
    key_fd, key_path = tempfile.mkstemp(suffix=".pem")

    with os.fdopen(cert_fd, "wb") as f:
        f.write(leaf_cert.public_bytes(serialization.Encoding.PEM))
    with os.fdopen(key_fd, "wb") as f:
        f.write(leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ))

    _cert_cache[hostname] = (cert_path, key_path)
    return cert_path, key_path


# ── Original destination lookup ──────────────────────────────────────────────

SO_ORIGINAL_DST = 80


def _get_original_dest(sock: socket.socket) -> tuple[str, int]:
    """Get the original destination of a redirected connection (via iptables REDIRECT)."""
    # SO_ORIGINAL_DST returns a sockaddr_in struct
    dst = sock.getsockopt(socket.SOL_IP, SO_ORIGINAL_DST, 16)
    port = struct.unpack("!H", dst[2:4])[0]
    ip = socket.inet_ntoa(dst[4:8])
    return ip, port


# ── Proxy handler ────────────────────────────────────────────────────────────


async def _relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Relay data from reader to writer until EOF."""
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError, OSError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def _read_http_head(reader: asyncio.StreamReader) -> bytes:
    """Read an HTTP request head (up to and including the \\r\\n\\r\\n)."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = await reader.read(65536)
        if not chunk:
            break
        buf += chunk
        if len(buf) > 1_000_000:
            break
    return buf


class ProxyServer:
    def __init__(self, port: int, rules_path: str, ca_cert: str, ca_key: str) -> None:
        self.port = port
        self.rules_path = rules_path
        self.ca_cert = ca_cert
        self.ca_key = ca_key
        self.rules: dict = {}
        self._rules_mtime: float = 0

    def _reload_rules(self) -> None:
        """Reload rules if the file has changed."""
        try:
            mtime = os.path.getmtime(self.rules_path)
            if mtime != self._rules_mtime:
                self.rules = load_rules(self.rules_path)
                self._rules_mtime = mtime
        except OSError:
            pass

    async def handle_client(self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
        """Handle a single redirected connection."""
        try:
            # Get original destination from iptables REDIRECT
            sock = client_writer.get_extra_info("socket")
            if sock is None:
                client_writer.close()
                return

            orig_ip, orig_port = _get_original_dest(sock)
            self._reload_rules()

            if orig_port == 443:
                await self._handle_https(client_reader, client_writer, orig_ip, orig_port)
            else:
                await self._handle_http(client_reader, client_writer, orig_ip, orig_port)
        except Exception:
            pass
        finally:
            try:
                client_writer.close()
            except Exception:
                pass

    async def _handle_http(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        dest_ip: str,
        dest_port: int,
    ) -> None:
        """Handle plain HTTP: read request, inject headers, forward."""
        request_data = await _read_http_head(client_reader)
        if not request_data:
            return

        # Extract Host header
        host = self._extract_host(request_data, dest_ip)
        headers_to_inject = match_domain(host, self.rules)

        if headers_to_inject:
            request_data = inject_headers(request_data, headers_to_inject)

        # Connect to the real destination
        try:
            server_reader, server_writer = await asyncio.open_connection(dest_ip, dest_port)
        except Exception:
            return

        # Send the (possibly modified) request
        server_writer.write(request_data)
        await server_writer.drain()

        # Relay in both directions
        await asyncio.gather(
            _relay(server_reader, client_writer),
            _relay(client_reader, server_writer),
        )

    async def _handle_https(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        dest_ip: str,
        dest_port: int,
    ) -> None:
        """Handle HTTPS via transparent TLS interception."""
        # The connection was redirected by iptables, so the client thinks it's
        # talking directly to the server. We need to:
        # 1. Peek at the SNI to get the hostname
        # 2. Perform TLS handshake with the client using a generated cert
        # 3. Read the decrypted HTTP request
        # 4. Inject headers
        # 5. Forward to the real server over TLS

        # Get the raw socket for SNI extraction and TLS wrapping
        sock = client_writer.get_extra_info("socket")
        if sock is None:
            return

        # Extract SNI from the ClientHello
        hostname = await self._peek_sni(client_reader, sock)
        if not hostname:
            # Can't determine hostname — pass through without modification
            await self._passthrough(client_reader, client_writer, dest_ip, dest_port, sock)
            return

        headers_to_inject = match_domain(hostname, self.rules)
        if not headers_to_inject:
            # No rules for this domain — pass through without TLS interception
            await self._passthrough(client_reader, client_writer, dest_ip, dest_port, sock)
            return

        # Generate a leaf cert for this hostname
        try:
            cert_path, key_path = _generate_leaf_cert(hostname, self.ca_cert, self.ca_key)
        except Exception:
            await self._passthrough(client_reader, client_writer, dest_ip, dest_port, sock)
            return

        # We need to work at the socket level for TLS wrapping.
        # Detach from asyncio, work with raw sockets, then re-wrap.
        transport = client_writer.transport
        raw_sock = transport.get_extra_info("socket")
        if raw_sock is None:
            return

        # We need to get any buffered data from the reader
        # Since iptables REDIRECT gives us the raw TCP connection, the client
        # is about to send a TLS ClientHello. We may have already peeked at it.
        buffered = getattr(self, '_last_peek_data', b"")

        # Detach from asyncio
        transport.pause_reading()

        # Duplicate the socket so asyncio doesn't close it
        raw_fd = raw_sock.fileno()
        new_fd = os.dup(raw_fd)
        new_sock = socket.socket(fileno=new_fd)
        new_sock.setblocking(True)

        # Close the asyncio transport
        transport.close()

        try:
            await self._mitm_connection(new_sock, buffered, hostname, dest_ip, dest_port, cert_path, key_path, headers_to_inject)
        except Exception:
            pass
        finally:
            try:
                new_sock.close()
            except Exception:
                pass

    async def _mitm_connection(
        self,
        client_sock: socket.socket,
        initial_data: bytes,
        hostname: str,
        dest_ip: str,
        dest_port: int,
        cert_path: str,
        key_path: str,
        headers_to_inject: dict[str, str],
    ) -> None:
        """Perform TLS MITM: terminate client TLS, inject headers, re-encrypt to server."""
        loop = asyncio.get_event_loop()

        def _do_mitm():
            # Create server-side SSL context (we act as server to the client)
            server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_ctx.load_cert_chain(cert_path, key_path)

            # If we have initial_data (the ClientHello we peeked), we need to
            # feed it back. Since we duplicated the socket and the original
            # transport is closed, the data is lost. We use a MemoryBIO approach.
            # Actually, with iptables REDIRECT + dup, the socket should still
            # have the pending data since we only peeked with MSG_PEEK.
            try:
                client_tls = server_ctx.wrap_socket(client_sock, server_side=True)
            except ssl.SSLError:
                return

            # Connect to the real server
            server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server_sock.settimeout(10)
            try:
                server_sock.connect((dest_ip, dest_port))
            except Exception:
                client_tls.close()
                return

            client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            client_ctx.check_hostname = True
            client_ctx.load_default_certs()
            try:
                server_tls = client_ctx.wrap_socket(server_sock, server_hostname=hostname)
            except ssl.SSLError:
                client_tls.close()
                server_sock.close()
                return

            try:
                # Read the HTTP request from the client (decrypted)
                request = b""
                client_tls.settimeout(10)
                while b"\r\n\r\n" not in request:
                    chunk = client_tls.recv(65536)
                    if not chunk:
                        break
                    request += chunk
                    if len(request) > 1_000_000:
                        break

                if not request:
                    return

                # Inject headers
                request = inject_headers(request, headers_to_inject)

                # Send to real server
                server_tls.sendall(request)

                # Relay response back
                while True:
                    try:
                        data = server_tls.recv(65536)
                        if not data:
                            break
                        client_tls.sendall(data)
                    except (ssl.SSLError, OSError):
                        break
            finally:
                try:
                    client_tls.close()
                except Exception:
                    pass
                try:
                    server_tls.close()
                except Exception:
                    pass

        await loop.run_in_executor(None, _do_mitm)

    async def _peek_sni(self, reader: asyncio.StreamReader, sock: socket.socket) -> str | None:
        """Peek at the TLS ClientHello to extract the SNI hostname."""
        try:
            # Use MSG_PEEK to look at the data without consuming it
            raw_sock = sock
            raw_sock.setblocking(False)
            loop = asyncio.get_event_loop()

            data = await loop.run_in_executor(
                None, lambda: raw_sock.recv(4096, socket.MSG_PEEK)
            )
            raw_sock.setblocking(False)
            self._last_peek_data = data

            if len(data) < 5:
                return None
            # Check TLS record
            if data[0] != 0x16:  # Not a handshake
                return None
            # Parse enough to find SNI
            return self._parse_sni(data)
        except Exception:
            return None

    @staticmethod
    def _parse_sni(data: bytes) -> str | None:
        """Parse SNI from a TLS ClientHello message."""
        try:
            if len(data) < 5:
                return None
            # TLS record header
            content_type = data[0]
            if content_type != 0x16:
                return None

            record_len = int.from_bytes(data[3:5], "big")
            pos = 5

            if pos >= len(data):
                return None
            # Handshake type
            if data[pos] != 0x01:  # ClientHello
                return None
            pos += 1

            # Handshake length (3 bytes)
            if pos + 3 > len(data):
                return None
            pos += 3

            # Client version (2 bytes)
            if pos + 2 > len(data):
                return None
            pos += 2

            # Random (32 bytes)
            if pos + 32 > len(data):
                return None
            pos += 32

            # Session ID
            if pos + 1 > len(data):
                return None
            session_id_len = data[pos]
            pos += 1 + session_id_len

            # Cipher suites
            if pos + 2 > len(data):
                return None
            cipher_suites_len = int.from_bytes(data[pos:pos + 2], "big")
            pos += 2 + cipher_suites_len

            # Compression methods
            if pos + 1 > len(data):
                return None
            comp_methods_len = data[pos]
            pos += 1 + comp_methods_len

            # Extensions
            if pos + 2 > len(data):
                return None
            extensions_len = int.from_bytes(data[pos:pos + 2], "big")
            pos += 2

            end = pos + extensions_len
            while pos + 4 <= end and pos + 4 <= len(data):
                ext_type = int.from_bytes(data[pos:pos + 2], "big")
                ext_len = int.from_bytes(data[pos + 2:pos + 4], "big")
                pos += 4

                if ext_type == 0x00:  # SNI extension
                    if pos + 2 > len(data):
                        return None
                    sni_list_len = int.from_bytes(data[pos:pos + 2], "big")
                    sni_pos = pos + 2
                    sni_end = sni_pos + sni_list_len

                    while sni_pos + 3 <= sni_end and sni_pos + 3 <= len(data):
                        name_type = data[sni_pos]
                        name_len = int.from_bytes(data[sni_pos + 1:sni_pos + 3], "big")
                        sni_pos += 3
                        if name_type == 0x00 and sni_pos + name_len <= len(data):
                            return data[sni_pos:sni_pos + name_len].decode("ascii")
                        sni_pos += name_len
                    return None

                pos += ext_len

            return None
        except Exception:
            return None

    async def _passthrough(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        dest_ip: str,
        dest_port: int,
        sock: socket.socket,
    ) -> None:
        """Pass traffic through without modification (no TLS interception)."""
        try:
            server_reader, server_writer = await asyncio.open_connection(dest_ip, dest_port)
        except Exception:
            return

        # Forward any peeked data
        peeked = getattr(self, '_last_peek_data', b"")
        if peeked:
            server_writer.write(peeked)
            await server_writer.drain()
            # Consume the peeked data from the socket
            try:
                sock.recv(len(peeked))
            except Exception:
                pass

        await asyncio.gather(
            _relay(client_reader, server_writer),
            _relay(server_reader, client_writer),
        )

    @staticmethod
    def _extract_host(request_data: bytes, default: str) -> str:
        """Extract the Host header value from raw HTTP request data."""
        lines = request_data.split(b"\r\n")
        for line in lines[1:]:
            if line.lower().startswith(b"host:"):
                host = line.split(b":", 1)[1].strip().decode("latin-1")
                # Remove port if present
                if ":" in host:
                    host = host.split(":")[0]
                return host
        return default

    async def run(self) -> None:
        """Start the proxy server."""
        self._reload_rules()
        server = await asyncio.start_server(
            self.handle_client, "0.0.0.0", self.port,
        )
        async with server:
            await server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="Flint credential injection proxy")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--rules", required=True)
    parser.add_argument("--ca-cert", required=True)
    parser.add_argument("--ca-key", required=True)
    args = parser.parse_args()

    proxy = ProxyServer(args.port, args.rules, args.ca_cert, args.ca_key)
    asyncio.run(proxy.run())


if __name__ == "__main__":
    main()

"""Credential injection proxy for Flint sandboxes.

Runs a lightweight forward proxy inside a VM's network namespace that intercepts
outbound HTTP/HTTPS requests and injects headers based on domain-matching rules.

The proxy is launched as a subprocess so it runs in its own process and can be
managed independently of the daemon's event loop.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile

from .config import log, PROXY_PORT, PROXY_CA_DIR
from ._netns import _popen_in_ns


def _ensure_ca() -> tuple[str, str]:
    """Ensure the proxy CA certificate and key exist. Returns (cert_path, key_path)."""
    os.makedirs(PROXY_CA_DIR, exist_ok=True)
    cert_path = os.path.join(PROXY_CA_DIR, "ca.pem")
    key_path = os.path.join(PROXY_CA_DIR, "ca-key.pem")

    if os.path.exists(cert_path) and os.path.exists(key_path):
        return cert_path, key_path

    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    import datetime

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "Flint Credential Proxy CA"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Flint"),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
        .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, key_cert_sign=True, crl_sign=True,
            content_commitment=False, key_encipherment=False,
            data_encipherment=False, key_agreement=False,
            encipher_only=False, decipher_only=False,
        ), critical=True)
        .sign(key, hashes.SHA256())
    )

    with open(key_path, "wb") as f:
        f.write(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ))
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))

    log.info("Generated proxy CA certificate at %s", cert_path)
    return cert_path, key_path


class CredentialProxy:
    """Manages a credential injection proxy for a single sandbox."""

    def __init__(self, ns_name: str) -> None:
        self.ns_name = ns_name
        self._process: subprocess.Popen | None = None
        self._rules_file: str | None = None

    def start(self, rules: dict) -> None:
        """Start the proxy subprocess in the sandbox's network namespace."""
        ca_cert, ca_key = _ensure_ca()

        # Write rules to a temp file
        fd, rules_path = tempfile.mkstemp(prefix="flint-proxy-rules-", suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(rules, f)
        self._rules_file = rules_path

        # Launch the proxy server as a subprocess inside the netns
        proxy_module = os.path.join(os.path.dirname(__file__), "_proxy_server.py")
        cmd = [
            sys.executable, proxy_module,
            "--port", str(PROXY_PORT),
            "--rules", rules_path,
            "--ca-cert", ca_cert,
            "--ca-key", ca_key,
        ]
        self._process = _popen_in_ns(
            self.ns_name, cmd,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        log.info("[%s] Credential proxy started (pid=%d)", self.ns_name, self._process.pid)

    def stop(self) -> None:
        """Stop the proxy subprocess."""
        if self._process:
            try:
                self._process.send_signal(signal.SIGTERM)
                self._process.wait(timeout=3)
            except Exception:
                try:
                    self._process.kill()
                    self._process.wait(timeout=2)
                except Exception:
                    pass
            log.info("[%s] Credential proxy stopped", self.ns_name)
            self._process = None
        if self._rules_file:
            try:
                os.unlink(self._rules_file)
            except OSError:
                pass
            self._rules_file = None

    def update_rules(self, rules: dict) -> None:
        """Update the proxy rules by rewriting the rules file.

        The proxy server watches this file for changes.
        """
        if self._rules_file:
            tmp = self._rules_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(rules, f)
            os.rename(tmp, self._rules_file)
            log.info("[%s] Credential proxy rules updated", self.ns_name)

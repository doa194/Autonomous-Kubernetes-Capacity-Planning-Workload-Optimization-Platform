"""S07 L3: the public API reaches governance only over mutual TLS.

Governance serves privileged routes on a listener that refuses clients without a certificate from
the platform CA. These tests run the API's real relay against a real TLS listener with the same
rule (throwaway CA and certificates), so they fail if the relay stops presenting its certificate —
for example after an HTTP client upgrade changes how certificates are passed — and they check that
a failed handshake becomes a typed `unavailable` error instead of an internal error.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import json
import ssl
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from capacity_platform.api.app import Relay
from capacity_platform.contracts.errors import ErrorCategory, PlatformError

_KEY_USAGE = (
    "digital_signature",
    "content_commitment",
    "key_encipherment",
    "data_encipherment",
    "key_agreement",
    "key_cert_sign",
    "crl_sign",
    "encipher_only",
    "decipher_only",
)


def _key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def _cert(subject: str, key, issuer: x509.Certificate | None, issuer_key, *, ca: bool, usage=None, san=None):
    now = dt.datetime.now(dt.UTC)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)])
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(issuer.subject if issuer else name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        # Python 3.13 verifies strictly: chains must be linkable by key identifiers.
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
    )
    if ca:
        signing = {k: k in ("key_cert_sign", "crl_sign") for k in _KEY_USAGE}
        builder = builder.add_extension(x509.KeyUsage(**signing), critical=True)
    if issuer is not None:
        builder = builder.add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer.public_key()), critical=False
        )
    if usage:
        builder = builder.add_extension(x509.ExtendedKeyUsage(usage), critical=False)
    if san:
        builder = builder.add_extension(x509.SubjectAlternativeName(san), critical=False)
    return builder.sign(issuer_key, hashes.SHA256())


def _write(path: Path, cert: x509.Certificate, key=None) -> tuple[str, str | None]:
    path.with_suffix(".crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    if key is None:
        return str(path.with_suffix(".crt")), None
    path.with_suffix(".key").write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return str(path.with_suffix(".crt")), str(path.with_suffix(".key"))


@pytest.fixture(scope="module")
def pki(tmp_path_factory) -> dict[str, tuple[str, str | None]]:
    d = tmp_path_factory.mktemp("pki")
    ca_key = _key()
    ca = _cert("test-ca", ca_key, None, ca_key, ca=True)
    server_key, client_key = _key(), _key()
    server = _cert(
        "governance",
        server_key,
        ca,
        ca_key,
        ca=False,
        usage=[ExtendedKeyUsageOID.SERVER_AUTH],
        san=[x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))],
    )
    client = _cert("capacity-api", client_key, ca, ca_key, ca=False, usage=[ExtendedKeyUsageOID.CLIENT_AUTH])
    return {
        "ca": _write(d / "ca", ca),
        "server": _write(d / "server", server, server_key),
        "client": _write(d / "client", client, client_key),
    }


@pytest.fixture(scope="module")
def mtls_listener(pki) -> Iterator[str]:
    """A stand-in for governance's mTLS port: answers with the client certificate's common name."""

    class Handler(BaseHTTPRequestHandler):
        def do_PUT(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            peer = dict(x[0] for x in self.connection.getpeercert()["subject"])
            body = json.dumps({"peer": peer["commonName"]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(*pki["server"])
    ctx.load_verify_locations(pki["ca"][0])
    ctx.verify_mode = ssl.CERT_REQUIRED
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"https://localhost:{server.server_address[1]}"
    server.shutdown()


def test_relay_presents_client_certificate_to_mtls_listener(pki, mtls_listener):
    relay = Relay(mtls_listener, "https://ai.invalid", pki["ca"][0], pki["client"])  # type: ignore[arg-type]

    resp = relay.send("governance", "PUT", "/internal/v1/mode", "user-token", {"mode": "Governed"})

    assert resp.status_code == 200
    assert json.loads(resp.body) == {"peer": "capacity-api"}


def test_relay_without_client_certificate_is_refused_as_unavailable(pki, mtls_listener):
    relay = Relay(mtls_listener, "https://ai.invalid", pki["ca"][0], None)

    with pytest.raises(PlatformError) as err:
        relay.send("governance", "PUT", "/internal/v1/mode", "user-token", {"mode": "Governed"})

    assert err.value.category is ErrorCategory.UNAVAILABLE


def test_relay_with_unreadable_certificate_is_unavailable_not_internal(pki, tmp_path):
    relay = Relay("https://localhost:1", "https://ai.invalid", pki["ca"][0], (str(tmp_path / "gone.crt"), "k"))

    with pytest.raises(PlatformError) as err:
        relay.send("governance", "PUT", "/internal/v1/mode", "user-token", {})

    assert err.value.category is ErrorCategory.UNAVAILABLE

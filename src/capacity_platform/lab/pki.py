"""Offline root CA + cert-manager-managed intermediate (S08-ADR-002).

The root key is generated on the host, DPAPI-protected, and used only here to sign the
intermediate. Only the intermediate key goes into the cluster (as the cert-manager CA
issuer secret), so a cluster compromise cannot mint a new intermediate. The root
certificate (public) is the trust anchor distributed to clients.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from capacity_platform.lab.protect import read_secret, write_secret

ROOT_VALIDITY = dt.timedelta(days=365 * 5)
INTERMEDIATE_VALIDITY = dt.timedelta(days=180)


def _name(cn: str) -> x509.Name:
    return x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Capacity Lab (local)"),
            x509.NameAttribute(NameOID.COMMON_NAME, cn),
        ]
    )


def _self_signed_root(key: ec.EllipticCurvePrivateKey) -> x509.Certificate:
    now = dt.datetime.now(dt.UTC)
    return (
        x509.CertificateBuilder()
        .subject_name(_name("Capacity Lab Root CA"))
        .issuer_name(_name("Capacity Lab Root CA"))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + ROOT_VALIDITY)
        .add_extension(x509.BasicConstraints(ca=True, path_length=1), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        # Key identifiers let strict verifiers (Python 3.13+, OpenSSL X509_STRICT) link the chain.
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA384())
    )


def ensure_root(pki_dir: Path) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    key_file, cert_file = pki_dir / "root-ca.key.dpapi", pki_dir / "root-ca.crt"
    if key_file.exists():
        key = serialization.load_pem_private_key(read_secret(key_file), password=None)
        cert = x509.load_pem_x509_certificate(cert_file.read_bytes()) if cert_file.exists() else None
        try:
            cert and cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier)
        except x509.ExtensionNotFound:
            cert = None  # an older root without key identifiers is re-issued with the SAME key
        if cert is None:
            cert = _self_signed_root(key)  # type: ignore[arg-type]
            cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        return key, cert  # type: ignore[return-value]
    pki_dir.mkdir(parents=True, exist_ok=True)
    key = ec.generate_private_key(ec.SECP384R1())
    cert = _self_signed_root(key)
    write_secret(
        key_file,
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()),
    )
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return key, cert


def issue_intermediate(pki_dir: Path) -> tuple[bytes, bytes, bytes]:
    """Return (intermediate key PEM, intermediate cert PEM, root cert PEM)."""
    root_key, root_cert = ensure_root(pki_dir)
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name("Capacity Lab Intermediate CA"))
        .issuer_name(root_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + INTERMEDIATE_VALIDITY)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.NameConstraints(
                permitted_subtrees=[
                    x509.DNSName("svc"),
                    x509.DNSName("svc.cluster.local"),
                    x509.DNSName("localhost"),
                    x509.IPAddress(__import__("ipaddress").ip_network("127.0.0.0/8")),
                ],
                excluded_subtrees=None,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(root_key.public_key()), critical=False)
        .sign(root_key, hashes.SHA384())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    return key_pem, cert.public_bytes(serialization.Encoding.PEM), root_cert.public_bytes(serialization.Encoding.PEM)

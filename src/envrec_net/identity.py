"""A long-term identity: an Ed25519 key and a self-signed certificate for the pinned TLS channel. Made by `cryptography`; the private key is written once, under the per-OS
layer's owner-only protection, and is never logged, printed or sent."""

from __future__ import annotations

import datetime
import hashlib
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import NameOID

KEY_FILE, CERT_FILE = "key.pem", "cert.pem"


def node_id_of(public_raw: bytes) -> str:
    return "n-" + hashlib.sha256(public_raw).hexdigest()[:32]


def fingerprint_of(cert_der: bytes) -> str:
    h = hashlib.sha256(cert_der).hexdigest()
    return "sha256:" + ":".join(h[i:i + 4] for i in range(0, 32, 4))  # 8 groups of 4: long enough to compare by eye, the full hash is what is pinned


def public_raw_of_cert(cert_der: bytes) -> bytes:
    pub = x509.load_der_x509_certificate(cert_der).public_key()
    return pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def verify_signature(public_raw: bytes, signature: bytes, data: bytes) -> bool:
    try:
        ed25519.Ed25519PublicKey.from_public_bytes(public_raw).verify(signature, data)
        return True
    except (InvalidSignature, ValueError):
        return False


@dataclass(frozen=True)
class Identity:
    key: ed25519.Ed25519PrivateKey
    cert_der: bytes

    @property
    def public_raw(self) -> bytes:
        return self.key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)

    @property
    def node_id(self) -> str:
        return node_id_of(self.public_raw)

    @property
    def fingerprint(self) -> str:
        return fingerprint_of(self.cert_der)

    def sign(self, data: bytes) -> bytes:
        return self.key.sign(data)


def create(*, now: datetime.datetime | None = None) -> Identity:
    key = ed25519.Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, node_id_of(raw))])
    now = now or datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=3650)).sign(key, None))
    return Identity(key, cert.public_bytes(serialization.Encoding.DER))


def save(identity: Identity, directory: Path, layer) -> None:
    """Write the key and the certificate. The folder and both files are made owner-only BEFORE the key is written into them, so it is never readable by others, not even briefly."""
    directory.mkdir(parents=True, exist_ok=True)
    layer.protect_private(directory)
    kp, cp = directory / KEY_FILE, directory / CERT_FILE
    kp.touch(exist_ok=True)
    layer.protect_private(kp)
    kp.write_bytes(identity.key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    cp.write_bytes(x509.load_der_x509_certificate(identity.cert_der).public_bytes(serialization.Encoding.PEM))


def load(directory: Path) -> Identity | None:
    kp, cp = directory / KEY_FILE, directory / CERT_FILE
    if not (kp.is_file() and cp.is_file()):
        return None
    key = serialization.load_pem_private_key(kp.read_bytes(), None)  # the key file is stored unencrypted, protected by the per-OS layer
    cert = x509.load_pem_x509_certificate(cp.read_bytes())
    return Identity(key, cert.public_bytes(serialization.Encoding.DER))  # type: ignore[arg-type]


def load_or_create(directory: Path, layer) -> Identity:
    ident = load(directory)
    if ident is None:
        ident = create()
        save(ident, directory, layer)
    return ident

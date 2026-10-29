"""Mutual TLS 1.3 with pinned keys, from the standard library's `ssl`. There is no public CA and no trust on first use beyond the pairing the owner confirmed: a side trusts exactly the
certificates pinned at pairing, loaded as its only trust anchors, and the host name is not used (identity is the pinned key)."""

from __future__ import annotations

import asyncio
import ssl
from collections.abc import Iterable
from pathlib import Path

from envrec_net.identity import CERT_FILE, KEY_FILE


def _pem(der: bytes) -> str:
    return ssl.DER_cert_to_PEM_cert(der)


def server_context(identity_dir: Path, trusted: Iterable[bytes]) -> ssl.SSLContext:
    """The coordinator's one listening port. A client MAY come without a certificate (that is a node asking to pair, and only pairing messages are answered); a client WITH a certificate
    must carry one pinned at pairing, or the handshake fails. `add_trusted` pins a newly paired node on the live context."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.load_cert_chain(str(identity_dir / CERT_FILE), str(identity_dir / KEY_FILE))
    ctx.verify_mode = ssl.CERT_OPTIONAL
    for der in trusted:
        ctx.load_verify_locations(cadata=_pem(der))
    return ctx


def add_trusted(ctx: ssl.SSLContext, der: bytes) -> None:
    ctx.load_verify_locations(cadata=_pem(der))


def client_context(identity_dir: Path, pinned_server: bytes) -> ssl.SSLContext:
    """A paired node dialing its coordinator: presents its own certificate, accepts only the coordinator certificate pinned at pairing."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.load_cert_chain(str(identity_dir / CERT_FILE), str(identity_dir / KEY_FILE))
    ctx.load_verify_locations(cadata=_pem(pinned_server))
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.check_hostname = False
    return ctx


def pairing_client_context() -> ssl.SSLContext:
    """A node that has not paired yet cannot verify the coordinator's certificate. It does not pretend to: the channel is encrypted but unauthenticated, and the pairing exchange
    itself (a password-authenticated key exchange whose confirmation covers the certificate it saw) is what authenticates it. A man in the middle fails that confirmation."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def peer_cert_der(writer: asyncio.StreamWriter) -> bytes | None:
    obj = writer.get_extra_info("ssl_object")
    return obj.getpeercert(binary_form=True) if obj is not None else None

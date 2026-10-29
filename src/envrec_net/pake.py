"""The pairing key exchange. The 6-digit code is the password of a password-authenticated key exchange (SPAKE2, the vetted `spake2` library): both sides contribute a message,
the code never travels, and an observer or a man in the middle without the code learns nothing that lets them test guesses offline. Each code allows one online attempt.

What this module adds on top is only composition of standard primitives (HKDF and HMAC from `cryptography` and the standard library): a key confirmation in each direction, bound to the
whole transcript and to the TLS certificate the node actually saw, so a man in the middle who terminates TLS with his own certificate cannot produce a valid confirmation."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from spake2 import SPAKE2_A, SPAKE2_B
from spake2.spake2 import SPAKEError

ID_NODE, ID_COORDINATOR = b"mas-node", b"mas-coordinator"
INFO = b"mas pair v1"


def new_code() -> str:
    """A fresh random 6-digit code from the operating system's secure random source."""
    return f"{secrets.randbelow(1_000_000):06d}"


class Side:
    """One side of the exchange. The node is A, the coordinator is B."""

    def __init__(self, role: str, code: str) -> None:
        if role not in ("node", "coordinator") or not (code.isdigit() and len(code) == 6):
            raise ValueError("role is node or coordinator, and the code is 6 digits")
        cls = SPAKE2_A if role == "node" else SPAKE2_B
        self._s = cls(code.encode(), idA=ID_NODE, idB=ID_COORDINATOR)
        self.role = role
        self.message = self._s.start()
        self.key: bytes | None = None

    def finish(self, peer_message: bytes) -> None:
        try:
            self.key = self._s.finish(peer_message)
        except (SPAKEError, ValueError, KeyError) as exc:
            raise PakeError(f"the peer's message is not valid: {type(exc).__name__}") from None


class PakeError(Exception):
    pass


def transcript(msg_a: bytes, msg_b: bytes, coordinator_cert_der: bytes) -> bytes:
    """A hash of everything both sides said and of the certificate the node saw on the channel."""
    h = hashlib.sha256()
    for part in (INFO, msg_a, msg_b, hashlib.sha256(coordinator_cert_der).digest()):
        h.update(len(part).to_bytes(4, "big") + part)
    return h.digest()


def confirmation_keys(shared_key: bytes, tr: bytes) -> tuple[bytes, bytes]:
    okm = HKDF(algorithm=hashes.SHA256(), length=64, salt=tr, info=INFO + b" confirm").derive(shared_key)
    return okm[:32], okm[32:]  # (coordinator's, node's)


def _canon(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def mac(key: bytes, label: bytes, tr: bytes, body: bytes = b"") -> bytes:
    return hmac.new(key, label + b"\x00" + tr + b"\x00" + hashlib.sha256(body).digest(), hashlib.sha256).digest()


def coordinator_confirm(k_coord: bytes, tr: bytes, coordinator_identity: dict) -> bytes:
    return mac(k_coord, b"coordinator", tr, _canon(coordinator_identity))


def node_confirm(k_node: bytes, tr: bytes, node_identity: dict) -> bytes:
    return mac(k_node, b"node", tr, _canon(node_identity))


def result_mac(k_coord: bytes, tr: bytes, accepted: bool) -> bytes:
    return mac(k_coord, b"result", tr, b"accept" if accepted else b"refuse")


def equal(a: bytes, b: bytes) -> bool:
    return hmac.compare_digest(a, b)

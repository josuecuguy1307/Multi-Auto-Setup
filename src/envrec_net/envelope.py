"""The envelope every message travels in: version, sender id, message type, payload, timestamp, nonce, Ed25519 signature over everything else (canonical JSON).

`Verifier.open` rejects, with a typed code and nothing else done: an unsigned message, a bad signature, an unknown version, an unknown sender, a replayed nonce, a timestamp
outside a small window, a malformed message. The clock is injectable so every rule is testable without waiting."""

from __future__ import annotations

import base64
import json
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from envrec_net.identity import Identity, node_id_of, verify_signature

VERSION = 1
WINDOW_S = 30.0  # a message older or newer than this (against the receiver's clock) is refused
MAX_PEERS_NONCES = 4096

REASONS = ("malformed", "unsigned", "unknown_version", "unknown_sender", "bad_signature", "stale_timestamp", "replayed_nonce", "wrong_sender")


class EnvelopeRejected(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


@dataclass(frozen=True)
class Envelope:
    version: int
    sender: str
    type: str
    payload: dict
    ts: float
    nonce: str
    signature: str

    def to_dict(self) -> dict:
        return {"version": self.version, "sender": self.sender, "type": self.type, "payload": self.payload, "ts": self.ts, "nonce": self.nonce, "signature": self.signature}


def canonical(d: dict[str, Any]) -> bytes:
    """The bytes that are signed: every field except the signature, keys sorted, no spaces, UTF-8."""
    body = {k: v for k, v in d.items() if k != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def seal(identity: Identity, type: str, payload: dict, *, clock: Callable[[], float] = time.time) -> dict:
    d = {"version": VERSION, "sender": identity.node_id, "type": type, "payload": payload, "ts": clock(), "nonce": secrets.token_hex(16)}
    d["signature"] = base64.b64encode(identity.sign(canonical(d))).decode()
    return d


class Verifier:
    """Opens envelopes. `peers(sender_id)` returns the pinned public key of a paired sender, or None."""

    def __init__(self, peers: Callable[[str], bytes | None], *, clock: Callable[[], float] = time.time, window_s: float = WINDOW_S) -> None:
        self.peers, self.clock, self.window_s = peers, clock, window_s
        self._seen: dict[str, dict[str, float]] = {}  # sender -> nonce -> time first seen

    def open(self, raw: Any, *, expect_sender: str | None = None) -> Envelope:
        if not isinstance(raw, dict):
            raise EnvelopeRejected("malformed", "not an object")
        sig = raw.get("signature")
        if not sig:
            raise EnvelopeRejected("unsigned")
        try:
            version, sender, typ, payload, ts, nonce = raw["version"], raw["sender"], raw["type"], raw["payload"], raw["ts"], raw["nonce"]
            if not (isinstance(sender, str) and isinstance(typ, str) and isinstance(payload, dict) and isinstance(nonce, str) and isinstance(ts, (int, float)) and not isinstance(ts, bool)):
                raise TypeError
            signature = base64.b64decode(sig, validate=True)
        except (KeyError, TypeError, ValueError):
            raise EnvelopeRejected("malformed", "a field is missing or has the wrong type") from None
        if version != VERSION:
            raise EnvelopeRejected("unknown_version", str(version)[:20])
        pub = self.peers(sender)
        if pub is None or node_id_of(pub) != sender:
            raise EnvelopeRejected("unknown_sender")
        if expect_sender is not None and sender != expect_sender:
            raise EnvelopeRejected("wrong_sender", "the envelope names a paired sender other than the one that owns this connection")
        if not verify_signature(pub, signature, canonical(raw)):
            raise EnvelopeRejected("bad_signature")
        now = self.clock()
        if abs(now - float(ts)) > self.window_s:
            raise EnvelopeRejected("stale_timestamp", f"{abs(now - float(ts)):.0f} s off")
        seen = self._seen.setdefault(sender, {})
        for n in [n for n, t in seen.items() if now - t > 2 * self.window_s]:  # a nonce older than twice the window cannot be replayed: its timestamp is already stale
            del seen[n]
        if nonce in seen:
            raise EnvelopeRejected("replayed_nonce")
        if len(seen) >= MAX_PEERS_NONCES:
            raise EnvelopeRejected("replayed_nonce", "too many messages in the window")
        seen[nonce] = now  # remembered only after the signature held: an attacker cannot fill the table
        return Envelope(version, sender, typ, payload, float(ts), nonce, sig)

"""Freshness inside a session without anybody's clock (docs/NETWORK_CONTRACT.md 3.6).

After the pinned TLS handshake the coordinator sends a random challenge; the node answers it signed (its `hello`). The challenge is the session id: every envelope of the session carries it
(`sid`) and a counter (`seq`) that only grows, in each direction, and both are covered by the signature. So an envelope recorded in another session fails on `sid`, a repeated or old one
fails on `seq`, and a repeated envelope nonce fails on the nonce: replay protection as strong as before, with no wall-clock comparison anywhere in this module. The wall clock is read
only by whoever wants to MEASURE skew (a warning, never a rejection).

REJECTION REASONS are typed; `PLAIN` says each one to the owner in plain words."""

from __future__ import annotations

import base64
import secrets
from dataclasses import dataclass
from typing import Any

from envrec_net.envelope import VERSION, Envelope, EnvelopeRejected, canonical
from envrec_net.identity import node_id_of, verify_signature

PROTOCOL = 2  # the session protocol: challenge, hello, then enveloped messages with sid and seq
MAX_NONCES = 8192

PLAIN = {
    "malformed": "A message was not in the expected form.",
    "unsigned": "A message arrived without a signature.",
    "bad_signature": "A message signature did not verify: this computer's key does not match the one the coordinator has for it.",
    "unknown_sender": "The message came from a key the other side does not know.",
    "wrong_sender": "The message named a different computer than the one on this connection.",
    "unknown_version": "The other side speaks a different message version.",
    "wrong_session": "A message belonged to another connection (a replay from an earlier session).",
    "replayed_sequence": "A message repeated or came out of order (a replay).",
    "replayed_nonce": "A message was sent twice (a replay).",
    "challenge_failed": "The reply to the coordinator's challenge was wrong.",
    "protocol_version": "This computer runs an older version of the program than the coordinator. Update it (for example `git pull`, then install again) and start the node again.",
    "unknown_type": "The message type is not one this side accepts.",
    "unknown_node": "The coordinator does not know this computer: it is not paired, or it was unpaired. Run `mas join` again.",
    "revoked": "The coordinator unpaired this computer. Run `mas join` to pair it again.",
    "busy": "The other side is handling as many tasks as it allows.",
}


def new_challenge() -> str:
    return secrets.token_hex(16)


@dataclass
class SessionVerifier:
    """Opens the envelopes of ONE session from ONE peer. Has no clock on purpose."""

    peer_id: str
    peer_public: bytes
    sid: str | None = None  # the node learns it from the coordinator's challenge; the coordinator chose it
    last_seq: int = 0

    def __post_init__(self) -> None:
        self._nonces: set[str] = set()

    def open(self, raw: Any, *, first: bool = False) -> Envelope:
        """`first=True` lets the node adopt the session id from the coordinator's challenge (its signature still has to verify)."""
        if not isinstance(raw, dict):
            raise EnvelopeRejected("malformed", "not an object")
        sig = raw.get("signature")
        if not sig:
            raise EnvelopeRejected("unsigned")
        try:
            version, sender, typ, payload, ts, nonce, sid, seq = raw["version"], raw["sender"], raw["type"], raw["payload"], raw["ts"], raw["nonce"], raw["sid"], raw["seq"]
            if not (isinstance(sender, str) and isinstance(typ, str) and isinstance(payload, dict) and isinstance(nonce, str) and isinstance(sid, str)
                    and isinstance(seq, int) and not isinstance(seq, bool) and isinstance(ts, (int, float))):
                raise TypeError
            signature = base64.b64decode(sig, validate=True)
        except (KeyError, TypeError, ValueError):
            raise EnvelopeRejected("malformed", "a field is missing or has the wrong type") from None
        if version != VERSION:
            raise EnvelopeRejected("unknown_version", str(version)[:20])
        if sender != self.peer_id or node_id_of(self.peer_public) != sender:
            raise EnvelopeRejected("wrong_sender")
        if not verify_signature(self.peer_public, signature, canonical(raw)):
            raise EnvelopeRejected("bad_signature")
        if self.sid is None and first:
            self.sid = sid
        if sid != self.sid:
            raise EnvelopeRejected("wrong_session")
        if seq <= self.last_seq:
            raise EnvelopeRejected("replayed_sequence", f"{seq} after {self.last_seq}")
        if nonce in self._nonces or len(self._nonces) >= MAX_NONCES:
            raise EnvelopeRejected("replayed_nonce")
        self._nonces.add(nonce)  # remembered only after the signature held: an attacker cannot fill the table
        self.last_seq = seq
        return Envelope(version, sender, typ, payload, float(ts), nonce, sig)


class Sealer:
    """Seals the envelopes this side sends in one session, numbering them 1, 2, 3, ..."""

    def __init__(self, identity, sid: str) -> None:
        self.identity, self.sid, self.seq = identity, sid, 0

    def seal(self, type: str, payload: dict) -> dict:
        from envrec_net.envelope import seal
        self.seq += 1
        return seal(self.identity, type, payload, sid=self.sid, seq=self.seq)

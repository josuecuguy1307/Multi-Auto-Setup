"""The discovery messages (docs/NETWORK_CONTRACT.md 1.8), standard library only. A node broadcasts a small versioned query from an ephemeral port; a coordinator answers by unicast.

The answer is a signed envelope carrying only: the coordinator's name, its LAN address, its TCP port, its identity fingerprint and the protocol version, plus the query's nonce. Before
pairing a node cannot check the signature (it has no key yet), so the answer is only a HINT: the pairing exchange and the owner's approval are what protect pairing. After pairing the node
holds the coordinator's pinned key and accepts nothing but an answer signed by it."""

from __future__ import annotations

import ipaddress
import json
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass

import base64

from envrec_net.envelope import canonical, seal
from envrec_net.identity import Identity, node_id_of, verify_signature

PROTOCOL = 1
DEFAULT_PORT = 7444
MAX_QUERY = 256
MAX_ANSWER = 1200
FIELDS = ("name", "address", "port", "fingerprint", "version", "nonce")


@dataclass(frozen=True)
class Answer:
    name: str
    address: str
    port: int
    fingerprint: str
    coordinator_id: str
    signed: bool  # True only when a pinned key verified the signature


def new_query() -> tuple[bytes, str]:
    nonce = secrets.token_hex(8)
    return json.dumps({"mas": "discover", "v": PROTOCOL, "nonce": nonce}, separators=(",", ":")).encode(), nonce


def parse_query(data: bytes) -> str | None:
    """The query's nonce, or None for anything that is not a well-formed version-1 query."""
    if not data or len(data) > MAX_QUERY:
        return None
    try:
        q = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(q, dict) or q.get("mas") != "discover" or q.get("v") != PROTOCOL:
        return None
    nonce = q.get("nonce")
    if not isinstance(nonce, str) or not 8 <= len(nonce) <= 32 or not nonce.isalnum():
        return None
    return nonce


def _clean(v, cap: int) -> str:
    return "".join(c for c in str(v) if c.isprintable())[:cap]


def make_answer(identity: Identity, nonce: str, *, name: str, address: str, port: int, clock: Callable[[], float] = time.time) -> bytes:
    payload = {"name": _clean(name, 60), "address": address, "port": int(port), "fingerprint": identity.fingerprint, "version": PROTOCOL, "nonce": nonce}
    data = json.dumps(seal(identity, "here", payload, clock=clock), separators=(",", ":")).encode()
    if len(data) > MAX_ANSWER:
        raise ValueError("a discovery answer must stay small")
    return data


class BadAnswer(Exception):
    pass


def parse_answer(data: bytes, nonce: str, *, pinned: tuple[str, bytes] | None = None) -> Answer:
    """Read one answer to OUR query. Without `pinned` it is a hint (`signed=False`). With `pinned` = (coordinator id, public key) from pairing, the signature must verify against that
    key and the sender must be that coordinator, or the answer is refused. Freshness comes from the query's nonce, which the answer must echo: no clock is read here."""
    if len(data) > MAX_ANSWER:
        raise BadAnswer("too large")
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise BadAnswer("not JSON") from None
    if not isinstance(raw, dict):
        raise BadAnswer("not an object")
    payload, sender = raw.get("payload"), raw.get("sender")
    if raw.get("type") != "here" or not isinstance(payload, dict) or not isinstance(sender, str):
        raise BadAnswer("not an answer")
    signed = False
    if pinned is not None:
        pid, pub = pinned
        try:
            sig = base64.b64decode(str(raw.get("signature", "")), validate=True)
        except ValueError:
            raise BadAnswer("rejected: unsigned") from None
        if sender != pid or node_id_of(pub) != pid:
            raise BadAnswer("rejected: not the pinned coordinator")
        if not verify_signature(pub, sig, canonical(raw)):
            raise BadAnswer("rejected: bad signature")
        signed = True
    if payload.get("nonce") != nonce or payload.get("version") != PROTOCOL:
        raise BadAnswer("not an answer to this query")
    try:
        port = payload["port"]
        address = str(ipaddress.ip_address(payload["address"]))
        if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
            raise ValueError
        ip = ipaddress.ip_address(address)
        if ip.version != 4 or not (ip.is_private or ip.is_link_local):
            raise ValueError
    except (KeyError, ValueError, TypeError):
        raise BadAnswer("bad address or port") from None
    return Answer(_clean(payload.get("name", ""), 60), address, port, _clean(payload.get("fingerprint", ""), 80), _clean(sender, 80), signed)

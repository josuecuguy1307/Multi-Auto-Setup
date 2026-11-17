"""Signed, single-use approvals bound to one exact action (guardrail 3, MISSION 4.1). Spec: DECISIONS.md D-004.

The gate (this module, running in the policy process, never in the model's process) opens a pending approval for a structured action: it
computes the action's hash, the hash of its undo, a short display summary, and a fresh random nonce kept only in memory. The key receives
those fields, shows the summary, waits for a physical press, and signs a fixed-width binary body (no JSON on the microcontroller):

    "MAS-APPROVE-v1\\0" | key_id[8] | counter u64 | session_id[32] | nonce[16] | action_hash[32] | undo_hash[32] | summary[32] | class u8 | press_window_s u8

followed by a pure Ed25519 signature (RFC 8032, SHA-512) over those 177 bytes. `Gate.consume` accepts a token once: the nonce is deleted on
success, a restart forgets every outstanding nonce, and expiry is measured on the gate's monotonic clock from `begin` (the key has no trusted
wall clock). The key's counter orders approvals for the log and detects gaps; it is not the replay defence.
"""

from __future__ import annotations

import dataclasses
import enum
import hashlib
import json
import math
import re
import secrets
import struct
import threading
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, NamedTuple

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

DOMAIN = b"MAS-APPROVE-v1\x00"
ACTION_DOMAIN = b"MAS-ACTION-v1\x00"
SESSION_DOMAIN = b"MAS-SESSION-v1\x00"
BODY = struct.Struct(">15s8sQ32s16s32s32s32sBB")
BODY_LEN = BODY.size
SIG_LEN = 64
MAX_TTL_S = 120.0
PRESS_WINDOW_S = 30
NO_UNDO = b"\x00" * 32
INT_LIMIT = 2**63 - 1
MAX_PENDING = 64  # outstanding approvals per class (a flood of one class never blocks another, e.g. a person's UNDO); expired ones are swept first
UNDO_KIND = re.compile(r"[a-z][a-z0-9_.-]{0,63}")

REASONS = ("malformed", "unknown_key", "bad_signature", "unknown_nonce", "wrong_session", "expired", "class_mismatch", "action_mismatch",
           "summary_mismatch", "counter_regressed", "no_undo", "too_many_pending")


class Class(enum.IntEnum):
    """What an approval is for. A token for one class is never valid for another (no cross-protocol reuse)."""
    WRITE = 1
    UNDO = 2
    JOIN_FLEET = 3
    EXPORT_DATA = 4
    NET_UP = 5
    LOG_HEAD = 6
    INSPECT_SESSION = 7


NEEDS_UNDO = frozenset({Class.WRITE, Class.JOIN_FLEET})


class Rejected(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        assert code in REASONS, code
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


# ---- the action: strict canonical form, hashed on the host only ---------------------------

def _check(v: Any, path: str = "$") -> None:
    if v is None or isinstance(v, bool):
        return
    if isinstance(v, int):
        if abs(v) > INT_LIMIT:
            raise Rejected("malformed", f"{path}: integer outside 64 bits")
        return
    if isinstance(v, str):
        if unicodedata.normalize("NFC", v) != v:
            raise Rejected("malformed", f"{path}: string is not NFC")
        return
    if isinstance(v, list):
        for i, x in enumerate(v):
            _check(x, f"{path}[{i}]")
        return
    if isinstance(v, dict):
        for k, x in v.items():
            if not isinstance(k, str):
                raise Rejected("malformed", f"{path}: non-string key")
            _check(k, f"{path}.<key>")
            _check(x, f"{path}.{k}")
        return
    if isinstance(v, float) and not math.isfinite(v):
        raise Rejected("malformed", f"{path}: non-finite number")
    raise Rejected("malformed", f"{path}: {type(v).__name__} is not allowed (no floats, bytes, sets or tuples)")


def canonical(obj: Any) -> bytes:
    """Sorted keys, no spaces, UTF-8. Only null, bool, 64-bit int, NFC string, list and object: nothing whose encoding could differ between implementations.
    Anything else (lone surrogates, nesting too deep to walk, unencodable values) is Rejected("malformed"), never another exception."""
    try:
        _check(obj)
        return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (UnicodeEncodeError, RecursionError, ValueError, TypeError) as exc:
        raise Rejected("malformed", type(exc).__name__) from None


def action_hash(action: dict) -> bytes:
    if not isinstance(action, dict):
        raise Rejected("malformed", "an action is an object")
    return hashlib.sha256(ACTION_DOMAIN + canonical(action)).digest()


def parse_action(text: str) -> dict:
    """Parse an action that arrived as JSON text; duplicate keys are refused, not silently resolved."""
    if not isinstance(text, str):
        raise Rejected("malformed", "an action arrives as text")
    def pairs(items):
        d = {}
        for k, v in items:
            if k in d:
                raise Rejected("malformed", f"duplicate key {k!r}")
            d[k] = v
        return d
    try:
        obj = json.loads(text, object_pairs_hook=pairs, parse_float=lambda s: float(s), parse_constant=lambda s: float("nan"))
    except (ValueError, RecursionError) as exc:
        raise Rejected("malformed", str(exc)[:100]) from None
    action_hash(obj)
    return obj


def as_class(value: Any) -> Class:
    """Exactly one of the listed integers: no bool, float, string or None standing in for a class."""
    if type(value) is not int and type(value) is not Class:  # no bool, float, str, None, foreign IntEnum or int subclass
        raise Rejected("class_mismatch", f"a class is an integer, not {type(value).__name__}")
    try:
        return Class(value)
    except ValueError:
        raise Rejected("class_mismatch", f"unknown class {value}") from None


def has_undo(action: dict) -> bool:
    """An undo is an object that names what it does (a non-empty "kind"); {} or a bare argument list is not an undo."""
    undo = action.get("undo")
    return isinstance(undo, dict) and isinstance(undo.get("kind"), str) and UNDO_KIND.fullmatch(undo["kind"]) is not None


def undo_hash_of(action: dict) -> bytes:
    undo = action.get("undo")
    return action_hash(undo) if isinstance(undo, dict) else NO_UNDO


def pad_summary(text: str) -> bytes:
    """Printable ASCII only (anything else becomes '?'), truncated or space-padded to 32 bytes."""
    raw = "".join(c if 0x20 <= ord(c) < 0x7F else "?" for c in text)
    return raw[:32].ljust(32).encode("ascii")


def summary_for(action: dict, cls: Class) -> bytes:
    """What the key shows: class, target, number of files, kind. Derived only from the structured action, so the gate can re-derive and compare it."""
    files = action.get("files")
    n = len(files) if isinstance(files, (dict, list)) else 0
    return pad_summary(f"{Class(cls).name[:6]} {str(action.get('target', '?'))[:12]} {n}f {action.get('kind', '?')}")


def session_id(session_pub: bytes, transcript: bytes) -> bytes:
    """Binds approvals to one session: the session's ephemeral public key and the pairing transcript."""
    return hashlib.sha256(SESSION_DOMAIN + len(session_pub).to_bytes(2, "big") + session_pub + transcript).digest()


def key_id_of(public_key: bytes) -> bytes:
    return hashlib.sha256(public_key).digest()[:8]


# ---- the wire body ------------------------------------------------------------------------

class KeyRequest(NamedTuple):
    """What the host sends the key. The key adds its key_id and counter and signs."""
    session_id: bytes
    nonce: bytes
    action_hash: bytes
    undo_hash: bytes
    summary: bytes
    cls: int
    press_window_s: int


class Body(NamedTuple):
    key_id: bytes
    counter: int
    req: KeyRequest


def pack_body(key_id: bytes, counter: int, r: KeyRequest) -> bytes:
    return BODY.pack(DOMAIN, key_id, counter, r.session_id, r.nonce, r.action_hash, r.undo_hash, r.summary, r.cls, r.press_window_s)


def unpack_body(body: bytes) -> Body:
    dom, kid, counter, sid, nonce, ah, uh, summary, cls, pw = BODY.unpack(body)
    if dom != DOMAIN:
        raise Rejected("malformed", "wrong domain tag")
    return Body(kid, counter, KeyRequest(sid, nonce, ah, uh, summary, cls, pw))


# ---- the gate -----------------------------------------------------------------------------

@dataclass(frozen=True)
class Pending:
    nonce: bytes
    session: bytes
    action_hash: bytes
    undo_hash: bytes
    summary: bytes
    cls: Class
    press_window_s: int
    created: float

    def key_request(self) -> KeyRequest:
        return KeyRequest(self.session, self.nonce, self.action_hash, self.undo_hash, self.summary, int(self.cls), self.press_window_s)


@dataclass(frozen=True)
class Approval:
    key_id: bytes
    counter: int
    cls: Class
    action_hash: bytes
    undo_hash: bytes
    nonce: bytes


class Gate:
    """Holds pending approvals in memory and accepts each signed token once. `pinned` maps key_id to the raw 32-byte Ed25519 public key."""

    def __init__(self, pinned: dict[bytes, bytes], *, session: bytes, clock: Callable[[], float] = time.monotonic, ttl_s: float = MAX_TTL_S) -> None:
        if type(session) not in (bytes, bytearray) or len(session) != 32:
            raise ValueError("session id is 32 bytes")
        session = bytes(session)
        try:
            ok = not isinstance(ttl_s, bool) and isinstance(ttl_s, (int, float)) and math.isfinite(ttl_s) and 0 < ttl_s <= MAX_TTL_S
        except OverflowError:
            ok = False
        if not ok:
            raise ValueError(f"ttl_s must be finite and in (0, {MAX_TTL_S}]")
        self._keys = {kid: Ed25519PublicKey.from_public_bytes(pub) for kid, pub in pinned.items() if key_id_of(pub) == kid}
        self.session, self.clock, self.ttl_s = session, clock, float(ttl_s)
        self._pending: dict[bytes, Pending] = {}
        self._last_counter: dict[bytes, int] = {}
        self._lock = threading.Lock()

    def _live(self, p: Pending, now: float) -> bool:
        """Fail closed: a clock that went back, a non-finite elapsed time or one past the TTL all mean expired."""
        elapsed = now - p.created
        return math.isfinite(elapsed) and 0 <= elapsed < self.ttl_s

    def _now(self) -> float:
        """One clock reading, refused unless it is a real finite number: a bad reading is never stored and never wedges the gate."""
        v = self.clock()
        try:
            if type(v) not in (int, float) or not math.isfinite(v):
                raise Rejected("expired", "the gate's clock gave an unusable reading")
            return float(v)
        except OverflowError:
            raise Rejected("expired", "the gate's clock reading is too large") from None

    def begin(self, action: dict, cls: Class) -> Pending:
        cls = as_class(cls)
        if not isinstance(action, dict):
            raise Rejected("malformed", "an action is an object")
        ah, uh = action_hash(action), undo_hash_of(action)
        if cls in NEEDS_UNDO and (uh == NO_UNDO or not has_undo(action)):
            raise Rejected("no_undo", "a write whose undo cannot be stated is never offered")
        summary = summary_for(action, cls)
        with self._lock:
            now = self._now()  # read under the lock: no begin can sweep with a time older than a pending another one just made
            # the sweep agrees with consume: whatever could never be consumed now (past its TTL, or from the future after a clock
            # rollback) is dropped and holds no slot of the cap
            for nonce in [n for n, q in self._pending.items() if not self._live(q, now)]:
                del self._pending[nonce]
            if sum(1 for q in self._pending.values() if q.cls == cls) >= MAX_PENDING:
                raise Rejected("too_many_pending", f"{MAX_PENDING} {cls.name} approvals are already waiting")
            p = Pending(secrets.token_bytes(16), self.session, ah, uh, summary, cls, PRESS_WINDOW_S, now)
            self._pending[p.nonce] = p
        return dataclasses.replace(p)  # a copy: the caller can never touch the gate's own record (its creation time, its hashes)

    def consume(self, token: Any, action: dict, *, cls: Class) -> Approval:
        if not isinstance(token, (bytes, bytearray)) or len(token) != BODY_LEN + SIG_LEN:
            raise Rejected("malformed", "a token is exactly %d bytes" % (BODY_LEN + SIG_LEN))
        token = bytes(token)
        if len(token) != BODY_LEN + SIG_LEN:  # its real length, whatever the object claimed
            raise Rejected("malformed", "a token is exactly %d bytes" % (BODY_LEN + SIG_LEN))
        body_bytes, sig = token[:BODY_LEN], token[BODY_LEN:]
        b = unpack_body(body_bytes)
        pub = self._keys.get(b.key_id)
        if pub is None:
            raise Rejected("unknown_key")
        try:
            pub.verify(sig, body_bytes)
        except InvalidSignature:
            raise Rejected("bad_signature") from None
        cls = as_class(cls)
        if not isinstance(action, dict):
            raise Rejected("malformed", "an action is an object")
        ah, uh = action_hash(action), undo_hash_of(action)  # before the lock: a malformed action raises here and burns nothing
        with self._lock:
            p = self._pending.get(b.req.nonce)
            if p is None:
                raise Rejected("unknown_nonce", "never issued here, already used, or forgotten at restart")
            if b.req.session_id != self.session or p.session != self.session:
                raise Rejected("wrong_session")
            if not self._live(p, self._now()):
                del self._pending[p.nonce]
                raise Rejected("expired")
            if b.req.cls != p.cls or cls != p.cls:
                raise Rejected("class_mismatch")
            if not (ah == b.req.action_hash == p.action_hash and uh == b.req.undo_hash == p.undo_hash):
                raise Rejected("action_mismatch")
            if not (b.req.summary == p.summary == summary_for(action, p.cls)) or b.req.press_window_s != p.press_window_s:
                raise Rejected("summary_mismatch", "the key was shown something other than this action")
            if b.counter <= self._last_counter.get(b.key_id, 0):
                raise Rejected("counter_regressed")
            del self._pending[p.nonce]
            self._last_counter[b.key_id] = b.counter
        return Approval(b.key_id, b.counter, p.cls, ah, uh, p.nonce)

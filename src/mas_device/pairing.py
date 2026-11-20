"""Pairing between one key and one portable medium, and the session consent shown at launch (DECISIONS D-004, D-021, D-032).

The device owner pairs a key with a medium once, at provisioning: `provision` writes `pairing.json` next to the runtime on the medium
(the medium is then exposed read-only, D-003). The record names exactly one key:

    {"key_id":"<16 hex>","key_pub":"<64 hex>","medium_id":"<32 hex>","v":1}\n      (canonical JSON, sorted keys, no spaces, LF)

The gate pins that key and no other: there is no command-line or RPC way to pin a key (`gated --pin` is gone). Every launch derives a
fresh session from the pairing:

    transcript = sha256("MAS-PAIR-v1\\0" | medium_id | key_pub)
    session_id = approval.session_id(session_pub, transcript)        session_pub: 32 fresh random bytes per launch (v0, D-032)

The record is read strictly: anything but the exact canonical bytes is refused, so a record can never carry a second key, a stray field
or a key_id that is not its key's.

TERMS is what the host's owner sees before the gate serves anything (D-021: their launch is the session's consent).

SIMULATED: in v0 the key is the Python key model and the medium a folder.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

from mas_device import approval as ap

PAIR_DOMAIN = b"MAS-PAIR-v1\0"
RECORD_NAME = "pairing.json"
VERSION = 1
MAX_RECORD = 512
_FIELDS = ("key_id", "key_pub", "medium_id", "v")
_HEX = {"key_id": 8, "key_pub": 32, "medium_id": 16}
_LOWER_HEX = re.compile(r"[0-9a-f]*")

TERMS = (
    "MAS portable device - terms of this session\n"
    "You (the owner of this computer) started this session yourself. Nothing on the device runs by itself.\n"
    "1. Read-only by default: the assistant can read files under the folder you chose, nothing else, and every read is logged.\n"
    "2. Each write needs one press of the device's button: one press approves one write, exactly the action shown on the device's "
    "screen, once, for a short time. Each write records how to undo it.\n"
    "3. The audit log of this session stays on the device and you can read it.\n"
    "4. To end the session, close this window or unplug the device. Nothing stays installed on this computer.\n"
)


class PairingError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code, detail)
        self.code, self.detail = code, detail

    def __str__(self) -> str:
        return f"pairing record: {self.code}" + (f" ({self.detail})" if self.detail else "")


@dataclass(frozen=True)
class Pairing:
    key_pub: bytes
    medium_id: bytes

    @property
    def key_id(self) -> bytes:
        return ap.key_id_of(self.key_pub)

    @property
    def transcript(self) -> bytes:
        return hashlib.sha256(PAIR_DOMAIN + self.medium_id + self.key_pub).digest()


def _check_bytes(v, n: int, what: str) -> bytes:
    if type(v) not in (bytes, bytearray) or len(v) != n:
        raise PairingError("malformed", f"{what} is {n} bytes")
    return bytes(v)


def record_bytes(key_pub: bytes, medium_id: bytes) -> bytes:
    key_pub, medium_id = _check_bytes(key_pub, 32, "key_pub"), _check_bytes(medium_id, 16, "medium_id")
    doc = {"key_id": ap.key_id_of(key_pub).hex(), "key_pub": key_pub.hex(), "medium_id": medium_id.hex(), "v": VERSION}
    return (json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")


def provision(medium: Path, key_pub: bytes) -> Path:
    """The device owner's pairing step: write the record once. An existing pairing is never overwritten (re-pairing is a new medium)."""
    key_pub = _check_bytes(key_pub, 32, "key_pub")
    medium = Path(medium)
    medium.mkdir(parents=True, exist_ok=True)
    path = medium / RECORD_NAME
    data = record_bytes(key_pub, secrets.token_bytes(16))
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o644)
    except FileExistsError:
        raise PairingError("already_paired", str(path)) from None
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    return path


def _no_dup(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            raise PairingError("malformed", f"duplicate field {k!r}")
        d[k] = v
    return d


def load(path: Path) -> Pairing:
    """Read a pairing record strictly. Exactly one key; its key_id must be its own; the bytes must be the canonical form."""
    path = Path(path)
    try:
        with open(path, "rb") as f:
            data = f.read(MAX_RECORD + 1)
    except FileNotFoundError:
        raise PairingError("pairing_missing", str(path)) from None
    except OSError as exc:
        raise PairingError("unreadable", type(exc).__name__) from None
    if len(data) > MAX_RECORD:
        raise PairingError("malformed", "too large")
    try:
        doc = json.loads(data.decode("ascii"), object_pairs_hook=_no_dup)
    except PairingError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise PairingError("malformed", "not an ASCII JSON object") from None
    if not isinstance(doc, dict) or tuple(sorted(doc)) != _FIELDS:
        raise PairingError("malformed", "fields are exactly " + ", ".join(_FIELDS))
    if type(doc["v"]) is not int or doc["v"] != VERSION:
        raise PairingError("malformed", f"version must be {VERSION}")
    raw = {}
    for name, n in _HEX.items():
        v = doc[name]
        if not isinstance(v, str) or len(v) != 2 * n or not _LOWER_HEX.fullmatch(v):
            raise PairingError("malformed", f"{name} is {2 * n} lowercase hex digits")
        raw[name] = bytes.fromhex(v)
    if ap.key_id_of(raw["key_pub"]) != raw["key_id"]:
        raise PairingError("key_id_mismatch", "the key_id is not this key's")
    if data != record_bytes(raw["key_pub"], raw["medium_id"]):
        raise PairingError("malformed", "not the canonical form")
    return Pairing(raw["key_pub"], raw["medium_id"])


def new_session(p: Pairing) -> tuple[bytes, bytes]:
    """A fresh session for one launch: (session_pub, session_id). Tokens of one session are refused in every other (D-004)."""
    session_pub = secrets.token_bytes(32)
    return session_pub, ap.session_id(session_pub, p.transcript)


def terms_sha256() -> str:
    return hashlib.sha256(TERMS.encode("utf-8")).hexdigest()

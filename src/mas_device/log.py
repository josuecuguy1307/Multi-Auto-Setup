"""The device's audit log (guardrail 5): append-only JSON lines, each record chained to the previous one by hash.

    record = {seq, prev, type, payload, at, hash}     hash = sha256("MAS-LOG-v1\\0" | canonical({seq, prev, type, payload, at}))

The file alone proves order and integrity of what it holds; truncation of the tail is detected against a known head (the gate keeps the head in
memory, and the key countersigns it with a LOG_HEAD approval: DECISIONS D-006). The logger re-checks the file's tail before every append and
refuses to extend a broken log (fail closed). The only write operation is `append`. SIMULATED: host filesystem; on the device the file lives
on the key/compute side, out of reach of the model's process.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from mas_device.approval import Rejected, canonical

DOMAIN = b"MAS-LOG-v1\x00"
GENESIS = "0" * 64
FIELDS = ("seq", "prev", "type", "payload", "at")


class LogBroken(Exception):
    pass


@dataclass(frozen=True)
class Head:
    seq: int
    hash: str


@dataclass(frozen=True)
class Verdict:
    ok: bool
    head: Head
    bad_seq: int | None = None
    reason: str = ""


def record_hash(rec: dict) -> str:
    return hashlib.sha256(DOMAIN + canonical({k: rec[k] for k in FIELDS})).hexdigest()


def _no_dup(items):
    d = {}
    for k, v in items:
        if k in d:
            raise ValueError(f"duplicate key {k!r}")
        d[k] = v
    return d


def _parse(line: str) -> dict:
    rec = json.loads(line, object_pairs_hook=_no_dup)
    if not isinstance(rec, dict) or set(rec) != set(FIELDS) | {"hash"}:
        raise ValueError("wrong fields")
    if not (isinstance(rec["seq"], int) and not isinstance(rec["seq"], bool) and isinstance(rec["at"], int) and not isinstance(rec["at"], bool)):
        raise ValueError("seq and at are integers")
    if not (isinstance(rec["type"], str) and isinstance(rec["payload"], dict) and isinstance(rec["prev"], str) and isinstance(rec["hash"], str)):
        raise ValueError("wrong field types")
    return rec


def _scan(data: bytes, expected: Head | None = None) -> Verdict:
    head = Head(0, GENESIS)
    if data and not data.endswith(b"\n"):
        return Verdict(False, head, head.seq + 1, "partial last line")
    for n, raw in enumerate(data.decode("utf-8", errors="strict").splitlines(), start=1):
        try:
            rec = _parse(raw)
            ok = rec["seq"] == n and rec["prev"] == head.hash and rec["hash"] == record_hash(rec)
        except (ValueError, Rejected, KeyError, TypeError):
            ok = False
        if not ok:
            return Verdict(False, head, n, f"record {n} does not chain")
        head = Head(n, rec["hash"])
    if expected is not None:
        if head.seq < expected.seq:
            return Verdict(False, head, head.seq + 1, f"truncated: file ends at {head.seq}, known head is {expected.seq}")
        if head.seq == expected.seq and head.hash != expected.hash:
            return Verdict(False, head, head.seq, "head hash differs from the known head")
    return Verdict(True, head)


def verify(path: Path, expected: Head | None = None) -> Verdict:
    p = Path(path)
    try:
        data = p.read_bytes()
    except FileNotFoundError:
        data = b""
    except UnicodeDecodeError:
        return Verdict(False, Head(0, GENESIS), 1, "not UTF-8")
    try:
        return _scan(data, expected)
    except UnicodeDecodeError:
        return Verdict(False, Head(0, GENESIS), 1, "not UTF-8")


class AuditLog:
    def __init__(self, path: Path, head: Head, size: int, clock: Callable[[], int]) -> None:
        self._path, self._head, self._size, self._clock = Path(path), head, size, clock
        self._lock = threading.Lock()

    @classmethod
    def open(cls, path: Path, *, clock: Callable[[], int] = lambda: time.time_ns() // 1_000_000) -> "AuditLog":
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        v = verify(p)
        if not v.ok:
            raise LogBroken(f"{p}: {v.reason}")
        return cls(p, v.head, p.stat().st_size if p.exists() else 0, clock)

    def head(self) -> Head:
        return self._head

    def append(self, type: str, payload: dict) -> Head:
        with self._lock:
            size = self._path.stat().st_size if self._path.exists() else 0
            if size != self._size:  # someone else wrote (or cut) the file since our last append
                raise LogBroken(f"{self._path}: changed behind the logger ({self._size} -> {size} bytes)")
            rec = {"seq": self._head.seq + 1, "prev": self._head.hash, "type": str(type), "payload": payload, "at": int(self._clock())}
            try:
                rec["hash"] = record_hash(rec)
            except Rejected as exc:
                raise LogBroken(f"payload is not canonical: {exc}") from None
            line = (json.dumps(rec, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
            fd = os.open(self._path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
            try:
                os.write(fd, line)
                os.fsync(fd)
            finally:
                os.close(fd)
            self._size += len(line)
            self._head = Head(rec["seq"], rec["hash"])
            return self._head

    def records(self) -> list[dict]:
        return [json.loads(l) for l in self._path.read_text(encoding="utf-8").splitlines()] if self._path.exists() else []


def render(path: Path) -> str:
    """The owners' view: one line per record, then the chain verdict."""
    p = Path(path)
    out = []
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        text = ""
    for raw in text.splitlines():
        try:
            r = json.loads(raw)
            out.append(f"#{r.get('seq')} {r.get('at')} {r.get('type')} {json.dumps(r.get('payload'), sort_keys=True)[:160]}")
        except ValueError:
            out.append(f"(unreadable line) {raw[:80]}")
    v = verify(p)
    out.append(f"chain: ok, {v.head.seq} records, head {v.head.hash[:16]}" if v.ok else f"chain: BROKEN at record {v.bad_seq} ({v.reason})")
    return "\n".join(out) + "\n"

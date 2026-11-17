"""The device's audit log (guardrail 5): append-only JSON lines, each record chained to the previous one by hash.

    record = {seq, prev, type, payload, at, hash}     hash = sha256("MAS-LOG-v1\\0" | canonical({seq, prev, type, payload, at}))
    line   = json.dumps(record, sort_keys=True, ensure_ascii=True) + "\\n"      (pure ASCII; verified byte for byte)

What the file alone proves: order and integrity of what it holds (any edit, deletion, reorder, reformat or extra field breaks the chain).
What it cannot prove alone: that nothing was cut off or rewritten as a whole. For that the reader passes a known head (`expected`): the file
must reach that seq, and its record at that seq must carry that hash, however long the file is now. Where the known head lives between runs
is the caller's job: today the policy gate keeps it in memory for one session. PLANNED, not built: the key countersigning the head with a
LOG_HEAD approval (DECISIONS D-006), which lets a later session check the head without trusting the host.

Writers: every append takes an OS lock on `<log>.lock`, re-reads the whole file and re-verifies it against the head this logger last wrote,
and refuses (LogBroken) if anything changed behind it; a second writer therefore gets an error, never a forked chain. Cost: O(file) per
append, fine for a session-sized log. A crash mid-append leaves a partial last line: the log is then refused until the owner opens it with
`repair_partial=True`, which completes a whole record that only lacks its newline, or drops an incomplete fragment, and logs either as a
`log.repaired` record. It never drops a complete record.

SIMULATED: host filesystem. "The model's process cannot write it" is NOT enforced here (any process of the same user can write the file;
mode 0o600 means nothing on Windows); that separation is the gate process boundary tested in P2-GUARDRAILS. This module only makes
tampering detectable.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from mas_device.approval import Rejected, canonical

DOMAIN = b"MAS-LOG-v1\x00"
GENESIS = "0" * 64
FIELDS = ("seq", "prev", "type", "payload", "at")
LOCK_TIMEOUT_S = 10.0


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


def _line(rec: dict) -> bytes:
    return (json.dumps(rec, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")


def _no_dup(items):
    d = {}
    for k, v in items:
        if k in d:
            raise ValueError(f"duplicate key {k!r}")
        d[k] = v
    return d


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _parse(raw: bytes) -> dict:
    if not raw.isascii():
        raise ValueError("not ASCII")
    rec = json.loads(raw.decode("ascii"), object_pairs_hook=_no_dup)
    if not isinstance(rec, dict) or set(rec) != set(FIELDS) | {"hash"}:
        raise ValueError("wrong fields")
    if not (_is_int(rec["seq"]) and _is_int(rec["at"])):
        raise ValueError("seq and at are integers")
    if not (isinstance(rec["type"], str) and isinstance(rec["payload"], dict) and isinstance(rec["prev"], str) and isinstance(rec["hash"], str)):
        raise ValueError("wrong field types")
    return rec


def _chains(raw: bytes, n: int, prev: str) -> dict | None:
    """The record on this line if it is record n after `prev`, hashed with the domain tag and written in the one canonical form; else None."""
    try:
        rec = _parse(raw)
        if rec["seq"] == n and rec["prev"] == prev and rec["hash"] == record_hash(rec) and _line(rec) == raw + b"\n":
            return rec
    except (ValueError, Rejected, KeyError, TypeError, UnicodeDecodeError):
        pass
    return None


def _scan(data: bytes, expected: Head | None = None) -> Verdict:
    head, hashes = Head(0, GENESIS), {0: GENESIS}
    chunks = data.split(b"\n")
    tail = chunks.pop()  # bytes after the last newline: empty for a well-formed file
    for n, raw in enumerate(chunks, start=1):
        rec = _chains(raw, n, head.hash)
        if rec is None:
            return Verdict(False, head, n, f"record {n} does not chain")
        head = Head(n, rec["hash"])
        hashes[n] = head.hash
    if tail:
        return Verdict(False, head, head.seq + 1, "partial last line")
    if expected is not None:
        if head.seq < expected.seq:
            return Verdict(False, head, head.seq + 1, f"truncated: file ends at {head.seq}, known head is {expected.seq}")
        if hashes[expected.seq] != expected.hash:
            return Verdict(False, head, expected.seq, f"record {expected.seq} differs from the known head: history was rewritten")
    return Verdict(True, head)


def _read(p: Path) -> bytes:
    try:
        return p.read_bytes()
    except FileNotFoundError:
        return b""


def verify(path: Path, expected: Head | None = None) -> Verdict:
    return _scan(_read(Path(path)), expected)


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    """An exclusive OS lock on `<log>.lock`, so the re-check and the write of one append are one step for every writer on this machine."""
    fd = os.open(path.with_name(path.name + ".lock"), os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
    try:
        if os.name == "nt":
            import msvcrt
            deadline = time.monotonic() + LOCK_TIMEOUT_S
            while True:
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise LogBroken(f"{path}: lock not acquired in {LOCK_TIMEOUT_S}s") from None
                    time.sleep(0.001)
            try:
                yield
            finally:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _write_end(p: Path, data: bytes) -> None:
    fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


class AuditLog:
    def __init__(self, path: Path, head: Head, size: int, clock: Callable[[], int]) -> None:
        self._path, self._head, self._size, self._clock = Path(path), head, size, clock
        self._lock = threading.Lock()

    @classmethod
    def open(cls, path: Path, *, clock: Callable[[], int] = lambda: time.time_ns() // 1_000_000, expected: Head | None = None,
             repair_partial: bool = False) -> "AuditLog":
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with _locked(p):
            data = _read(p)
            v = _scan(data, expected)
            if v.ok:
                return cls(p, v.head, len(data), clock)
            if not (repair_partial and v.reason == "partial last line"):
                raise LogBroken(f"{p}: {v.reason}")
            cut = data.rfind(b"\n") + 1
            fragment = data[cut:]
            whole = _chains(fragment, v.head.seq + 1, v.head.hash)
            if whole is not None:  # a complete record that only lost its newline: keep it
                _write_end(p, b"\n")
                head, note = Head(whole["seq"], whole["hash"]), {"completed_record": whole["seq"]}
            else:
                os.truncate(p, cut)
                head, note = v.head, {"dropped_bytes": len(fragment), "dropped_sha256": hashlib.sha256(fragment).hexdigest()}
            fixed = _read(p)
            vv = _scan(fixed, expected)
            if not vv.ok or vv.head != head:
                raise LogBroken(f"{p}: repair did not yield a valid chain ({vv.reason})")
            log = cls(p, head, len(fixed), clock)
            log._append_locked("log.repaired", note)
            return log

    def head(self) -> Head:
        return self._head

    def append(self, type: str, payload: dict) -> Head:
        with self._lock, _locked(self._path):
            return self._append_locked(type, payload)

    def _append_locked(self, type: str, payload: dict) -> Head:
        data = _read(self._path)
        if len(data) != self._size:
            raise LogBroken(f"{self._path}: changed behind the logger ({self._size} -> {len(data)} bytes)")
        v = _scan(data, self._head)
        if not v.ok or v.head != self._head:
            raise LogBroken(f"{self._path}: refusing to extend a log that no longer verifies ({v.reason or 'head moved'})")
        rec = {"seq": self._head.seq + 1, "prev": self._head.hash, "type": str(type), "payload": payload, "at": int(self._clock())}
        try:
            rec["hash"] = record_hash(rec)
        except Rejected as exc:
            raise LogBroken(f"payload is not canonical: {exc}") from None
        line = _line(rec)
        _write_end(self._path, line)
        self._size += len(line)
        self._head = Head(rec["seq"], rec["hash"])
        return self._head

    def records(self) -> list[dict]:
        """Every record, only if the file still verifies against the head this logger knows."""
        data = _read(self._path)
        v = _scan(data, self._head)
        if not v.ok:
            raise LogBroken(f"{self._path}: {v.reason}")
        return [json.loads(l) for l in data.split(b"\n")[:-1]]


def render(path: Path) -> str:
    """The owners' view: one line per record, then the chain verdict."""
    p = Path(path)
    out = []
    for raw in _read(p).split(b"\n"):
        if not raw:
            continue
        try:
            r = json.loads(raw.decode("utf-8"))
            out.append(f"#{r.get('seq')} {r.get('at')} {r.get('type')} {json.dumps(r.get('payload'), sort_keys=True)[:160]}")
        except (ValueError, UnicodeDecodeError, AttributeError):
            out.append(f"(unreadable line) {raw[:80]!r}")
    v = verify(p)
    out.append(f"chain: ok, {v.head.seq} records, head {v.head.hash[:16]}" if v.ok else f"chain: BROKEN at record {v.bad_seq} ({v.reason})")
    return "\n".join(out) + "\n"

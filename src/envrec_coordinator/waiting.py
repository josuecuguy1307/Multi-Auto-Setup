"""Machines waiting to be paired (docs/NETWORK_CONTRACT.md 1.8, like adding a printer). A node that discovered this coordinator and was picked by its owner dials in WITHOUT a certificate and says
who it is: `wait_hello` with a name, an operating system, an architecture and the fingerprint it claims. Nothing else is accepted before pairing. The entry lives while that connection lives
and for at most `TTL_S`; the number of entries is capped and each source is rate-limited. The OWNER picks one entry on the coordinator; only then does a 6-digit code exist, bound to that
entry's own connection (`PairingDesk` refuses it from any other)."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from envrec_net.wire import FrameError, write_frame

TTL_S = 600.0
CAP = 8
PER_SOURCE_CAP = 2
RATE = (5, 60.0)  # arrivals per source per minute
VERSION = 1
CLAIMED_FIELDS = {"name": 80, "os": 20, "arch": 20, "fingerprint": 80}


def _clean(v, cap: int) -> str:
    return "".join(c for c in str(v) if c.isprintable())[:cap]


@dataclass
class Waiting:
    id: str
    name: str
    os: str
    arch: str
    fingerprint: str  # claimed by the node; the pairing exchange is what authenticates it
    source: str
    since: float
    expires: float
    picked: asyncio.Event = field(default_factory=asyncio.Event)
    gone: bool = False

    def view(self) -> dict:
        return {"id": self.id, "name": self.name, "os": self.os, "arch": self.arch, "fingerprint": self.fingerprint, "since": self.since, "expires_at": self.expires}


class WaitingDesk:
    def __init__(self, *, clock: Callable[[], float] = time.time, log: Callable[[str, dict], None] = lambda t, p: None, ttl_s: float = TTL_S, cap: int = CAP,
                 on_change: Callable[[], None] = lambda: None) -> None:
        self.clock, self.log, self.ttl_s, self.cap, self.on_change = clock, log, ttl_s, cap, on_change
        self._entries: dict[str, Waiting] = {}
        self._arrivals: dict[str, deque[float]] = {}
        self._seq = 0

    def list(self) -> list[Waiting]:
        now = self.clock()
        return [w for w in self._entries.values() if not w.gone and w.expires >= now]

    def get(self, wait_id: str) -> Waiting | None:
        return next((w for w in self.list() if w.id == wait_id), None)

    def _allow(self, source: str) -> bool:
        q = self._arrivals.setdefault(source, deque())
        now = self.clock()
        while q and now - q[0] > RATE[1]:
            q.popleft()
        if len(q) >= RATE[0]:
            return False
        q.append(now)
        return True

    def admit(self, hello: dict, source: str) -> tuple[Waiting | None, str]:
        """Register a waiting machine from its hello, or (None, reason). Only the four claimed fields are read; anything else in the frame is ignored."""
        if hello.get("version") != VERSION:
            return None, "bad_hello"
        try:
            f = {k: _clean(hello[k], cap) for k, cap in CLAIMED_FIELDS.items()}
        except KeyError:
            return None, "bad_hello"
        if not f["name"] or not f["os"]:
            return None, "bad_hello"
        if not self._allow(source):
            return None, "rate_limited"
        live = self.list()
        if len(live) >= self.cap:
            return None, "waiting_full"
        if sum(1 for w in live if w.source == source) >= PER_SOURCE_CAP:
            return None, "waiting_full"
        self._seq += 1
        now = self.clock()
        w = Waiting(f"w{self._seq}-{int(now) % 100000}", f["name"], f["os"], f["arch"], f["fingerprint"], source, now, now + self.ttl_s)
        self._entries[w.id] = w
        self.log("waiting_added", {"waiting": w.id, "name": w.name, "os": w.os, "arch": w.arch, "source": source})
        self.on_change()
        return w, ""

    def remove(self, w: Waiting, reason: str) -> None:
        if w.id in self._entries:
            w.gone = True
            del self._entries[w.id]
            self.log("waiting_removed", {"waiting": w.id, "reason": reason})
            self.on_change()

    def pick(self, wait_id: str) -> Waiting | None:
        w = self.get(wait_id)
        if w is not None:
            w.picked.set()
        return w

    async def attend(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, source: str, hello: dict, *,
                    pair: Callable[..., Awaitable], cert_der: bytes, read_frame, pair_wait_s: float = 150.0) -> None:
        """One waiting connection from its hello to its end. Sends `wait_ack`, waits until the owner picks this entry (or the TTL, or the node hangs up), sends `pair_ready`, then hands the
        node's `pair_hello` to the pairing desk with this entry as the only machine the code is good for."""
        w, why = self.admit(hello, source)
        if w is None:
            self.log("waiting_refused", {"source": source, "reason": why})
            try:
                await write_frame(writer, {"type": "wait_error", "reason": why})
            except Exception:
                pass
            return
        try:
            await write_frame(writer, {"type": "wait_ack", "version": VERSION, "id": w.id, "ttl_s": self.ttl_s})
            hangup = asyncio.ensure_future(reader.read(1))  # the node says nothing while it waits: any data or an EOF ends the wait
            picked = asyncio.ensure_future(w.picked.wait())
            try:
                done, _ = await asyncio.wait({hangup, picked}, timeout=max(0.0, w.expires - self.clock()), return_when=asyncio.FIRST_COMPLETED)
            finally:
                for t in (hangup, picked):
                    if not t.done():
                        t.cancel()
                await asyncio.gather(hangup, picked, return_exceptions=True)  # the cancelled read must be gone before the next frame is read from this stream
            if picked not in done:
                reason = "the node left" if hangup in done else "waited too long"
                await write_frame(writer, {"type": "wait_error", "reason": "wait_expired"}) if hangup not in done else None
                self.remove(w, reason)
                return
            await write_frame(writer, {"type": "pair_ready", "version": VERSION})
            try:
                first = await read_frame(reader, timeout=pair_wait_s)
            except FrameError:
                self.remove(w, "no pairing attempt")
                return
            self.remove(w, "pairing started")
            await pair(reader, writer, source, cert_der, first_frame=first, bound_wait=w.id)
        except (ConnectionError, OSError, FrameError):
            self.remove(w, "connection lost")
        finally:
            self.remove(w, "closed")

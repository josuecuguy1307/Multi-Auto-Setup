"""The coordinator's side of pairing (protocol-pair). One offer at a time: a fresh random 6-digit code, valid two minutes, ONE attempt, shown only to the owner on the coordinator.

The exchange (frames over the coordinator's one TLS port, the client without a certificate): hello, reply (the coordinator's key confirmation), confirm (the node's key confirmation and
its identity), then the OWNER sees who is asking and says yes or no, then result. Everything that can go wrong ends the protocol in a terminal state, kills the code and stores
nothing. The code is never written anywhere but the offer in memory."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from envrec_coordinator.registry import Registry
from envrec_net import pake
from envrec_net.identity import Identity, fingerprint_of, node_id_of, public_raw_of_cert
from envrec_net.log import EventLog
from envrec_net.protocol import IllegalTransition, ProtocolGuard
from envrec_net.tls import peer_cert_der  # noqa: F401  (re-exported for tests)
from envrec_net.wire import FrameError, read_frame, write_frame

CODE_TTL_S = 120.0
OWNER_TTL_S = 120.0
HELLO_TIMEOUT_S = 10.0
VERSION = 1


@dataclass(frozen=True)
class Decision:
    """The owner's answer and where it came from (the terminal or the web screen). A plain bool is accepted too."""
    accept: bool
    via: str = ""


class PairingBusy(Exception):
    """A node is in the middle of pairing: a new offer would cut it off."""


AskOwner = Callable[[dict], Awaitable["bool | Decision"]]


class AttemptLimiter:
    """At most `max_attempts` pairing connections per source in `window_s`. Every connection that reaches the pairing door counts, whatever it does next."""

    def __init__(self, clock: Callable[[], float] = time.time, max_attempts: int = 3, window_s: float = 60.0) -> None:
        self.clock, self.max_attempts, self.window_s = clock, max_attempts, window_s
        self._hits: dict[str, deque[float]] = {}

    def allow(self, source: str) -> bool:
        now = self.clock()
        q = self._hits.setdefault(source, deque())
        while q and now - q[0] > self.window_s:
            q.popleft()
        if len(q) >= self.max_attempts:
            return False
        q.append(now)
        return True


@dataclass
class Offer:
    id: str
    code: str = field(repr=False)  # never printed, never logged
    created: float
    expires: float
    consumed: bool = False


@dataclass
class Outcome:
    status: str  # accepted | refused | failed | expired | rejected (never reached the exchange)
    reason: str = ""
    node_id: str | None = None


def _clean(v, cap: int) -> str:
    return "".join(c for c in str(v) if c.isprintable())[:cap]


class PairingDesk:
    def __init__(self, *, identity: Identity, registry: Registry, log: EventLog, ask_owner: AskOwner, on_paired: Callable[[bytes], None] = lambda der: None,
                 clock: Callable[[], float] = time.time, limiter: AttemptLimiter | None = None, owner_ttl_s: float = OWNER_TTL_S, on_outcome: Callable[[str, Outcome], None] = lambda i, o: None,
                 on_stage: Callable[[str, str], None] = lambda i, stage: None) -> None:
        self.identity, self.registry, self.log, self.ask_owner, self.on_paired = identity, registry, log, ask_owner, on_paired
        self.clock, self.limiter, self.owner_ttl_s, self.on_outcome, self.on_stage = clock, limiter or AttemptLimiter(clock), owner_ttl_s, on_outcome, on_stage
        self.offer: Offer | None = None
        self._guard: ProtocolGuard | None = None
        self._busy = False

    # ---- the owner asks for a code -------------------------------------------------------------------------------------------------------------

    def open_offer(self, offer_id: str, code: str) -> Offer:
        """A new offer replaces any earlier one (which is dead from this moment)."""
        now = self.clock()
        self.offer = Offer(offer_id, code, now, now + CODE_TTL_S)
        self._guard = ProtocolGuard("protocol-pair", lambda t, p: self.log.add(t, p), clock=self.clock, subject=offer_id)
        self.log.add("pair_offer_created", {"offer": offer_id, "valid_s": CODE_TTL_S})
        return self.offer

    @property
    def busy(self) -> bool:
        return self._busy

    def expire_due(self) -> None:
        """End a code nobody used in time, the same way `handle` does when a node arrives late, so a front end can show it as expired without waiting for a visitor."""
        self._live()

    def _live(self) -> Offer | None:
        o = self.offer
        if o is None or o.consumed:
            return None
        if self.clock() > o.expires:
            if self._guard is not None:
                self._guard.time_out("the code was not used within two minutes")
            self._finish(o, Outcome("expired", "the code was not used within two minutes"))
            return None
        return o

    def _finish(self, o: Offer, out: Outcome) -> None:
        o.consumed = True
        if self.offer is o:
            self.offer = None
        self.log.add(f"pair_{out.status}", {"offer": o.id, "reason": out.reason, **({"node": out.node_id} if out.node_id else {})}, owner=False)
        self.on_outcome(o.id, out)

    # ---- a connection without a certificate arrives ----------------------------------------------------------------------------------------------

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, source: str, coordinator_cert_der: bytes) -> Outcome:
        async def refuse(reason: str) -> Outcome:
            self.log.add("pair_refused", {"source": source, "reason": reason})
            try:
                await write_frame(writer, {"type": "pair_error", "reason": reason})
            except Exception:
                pass
            return Outcome("rejected", reason)

        if not self.limiter.allow(source):
            return await refuse("rate_limited")
        offer = self._live()
        if offer is None:
            return await refuse("no_active_code")
        if self._busy:
            return await refuse("pairing_in_progress")
        try:
            hello = await read_frame(reader, timeout=HELLO_TIMEOUT_S)
            msg_a = bytes.fromhex(str(hello["msg_a"]))
            if hello.get("type") != "pair_hello" or hello.get("version") != VERSION or not 1 < len(msg_a) < 200:
                raise ValueError
        except (FrameError, KeyError, ValueError, TypeError):
            return await refuse("bad_hello")  # a connection that sends no valid hello does not use the code up
        self._busy = True
        offer.consumed = True  # ONE attempt: from the first valid hello on, this code can never be tried again, whatever happens next
        self.log.add("pair_session_started", {"offer": offer.id, "source": source})
        self.on_stage(offer.id, "exchange")
        guard = self._guard
        assert guard is not None
        try:
            return await self._session(offer, guard, reader, writer, msg_a, coordinator_cert_der)
        finally:
            self._busy = False
            if self.offer is offer:
                self.offer = None

    async def _session(self, offer: Offer, guard: ProtocolGuard, reader, writer, msg_a: bytes, cert_der: bytes) -> Outcome:
        def end(status: str, reason: str, node_id: str | None = None) -> Outcome:
            out = Outcome(status, reason, node_id)
            self.log.add(f"pair_{status}", {"offer": offer.id, "reason": reason, **({"node": node_id} if node_id else {})})
            self.on_outcome(offer.id, out)
            return out

        try:
            guard.record("pair.hello", True)
            guard.advance("exchange")
            if guard.expire_if_due():
                return end("expired", "the code expired before the exchange")
            side = pake.Side("coordinator", offer.code)
            side.finish(msg_a)
            assert side.key is not None
            tr = pake.transcript(msg_a, side.message, cert_der)
            k_coord, k_node = pake.confirmation_keys(side.key, tr)
            me = {"id": self.identity.node_id, "cert_sha256": hashlib.sha256(cert_der).hexdigest()}
            await write_frame(writer, {"type": "pair_reply", "version": VERSION, "msg_b": side.message.hex(), "confirm": pake.coordinator_confirm(k_coord, tr, me).hex()})
            remaining = max(1.0, offer.expires - self.clock())
            try:
                frame = await read_frame(reader, timeout=remaining)
                node = frame["node"]
                node_identity = {"name": _clean(node["name"], 80), "os": _clean(node["os"], 20), "arch": _clean(node["arch"], 20), "cert_der_b64": str(node["cert_der_b64"])}
                cert = base64.b64decode(node_identity["cert_der_b64"], validate=True)
                public_raw_of_cert(cert)
                confirm = bytes.fromhex(str(frame["confirm"]))
                if frame.get("type") != "pair_confirm":
                    raise ValueError
            except (FrameError, KeyError, TypeError, ValueError, binascii.Error):
                guard.record("pair.key_confirmed", False)
                guard.advance("failed")
                return end("failed", "the node did not complete the exchange (wrong code, a man in the middle, or it gave up)")
            if not pake.equal(confirm, pake.node_confirm(k_node, tr, node_identity)):
                guard.record("pair.key_confirmed", False)
                guard.advance("failed")
                await self._say(writer, {"type": "pair_error", "reason": "confirmation_invalid"})
                return end("failed", "the node's key confirmation did not match (wrong code or a man in the middle)")
            guard.record("pair.key_confirmed", True)
            guard.advance("owner_confirm")
            summary = {"name": node_identity["name"], "os": node_identity["os"], "arch": node_identity["arch"], "fingerprint": fingerprint_of(cert), "offer": offer.id}
            try:
                answer = await asyncio.wait_for(self.ask_owner(summary), self.owner_ttl_s)
                accepted, via = bool(getattr(answer, "accept", answer)), getattr(answer, "via", "")
            except TimeoutError:
                guard.time_out("the owner did not answer")
                await self._say(writer, {"type": "pair_error", "reason": "owner_timeout"})
                return end("expired", "the owner did not answer in time")
            self.log.add("owner_decision", {"offer": offer.id, "accepted": accepted, "fingerprint": summary["fingerprint"], **({"via": via} if via else {})}, owner=True)
            guard.record("pair.owner_decision", "accept" if accepted else "refuse")
            guard.advance("accepted" if accepted else "refused")
            node_id = None
            if accepted:
                rec = self.registry.add(name=node_identity["name"], os_family=node_identity["os"], arch=node_identity["arch"], cert_der=cert, clock=self.clock)
                self.on_paired(cert)
                node_id = rec.node_id
            await self._say(writer, {"type": "pair_result", "accepted": bool(accepted), "mac": pake.result_mac(k_coord, tr, bool(accepted)).hex()})
            return end("accepted" if accepted else "refused", "the owner said " + ("yes" if accepted else "no"), node_id)
        except IllegalTransition as exc:
            return end("failed", f"illegal protocol move refused: {str(exc)[:100]}")
        except pake.PakeError as exc:
            if guard.state == "exchange":
                guard.record("pair.key_confirmed", False)
                try:
                    guard.advance("failed")
                except IllegalTransition:
                    pass
            return end("failed", str(exc)[:100])
        except (ConnectionError, OSError):
            return end("failed", "the connection dropped")

    @staticmethod
    async def _say(writer, frame: dict) -> None:
        try:
            await write_frame(writer, frame)
        except Exception:
            pass

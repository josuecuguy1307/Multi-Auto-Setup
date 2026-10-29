"""The coordinator's server. The ONE LAN-facing listening socket of the whole system is created here, by `asyncio.start_server` below, and nowhere else (conformance N2). With `web=True` the
coordinator also serves the owner's screen on ONE more socket, bound to loopback only (`web.py`); it is off by default.

A client without a certificate may only try to pair. A client with a certificate must carry one pinned at pairing (the TLS handshake fails otherwise); its messages are signed
envelopes, and anything unsigned, badly signed, replayed, stale, from an unknown sender or of an unknown type is logged and ends the connection."""

from __future__ import annotations

import asyncio
import ipaddress
import time
import uuid
from pathlib import Path

import envrec_os
from envrec_coordinator.control import ControlWatcher, write_json
from envrec_coordinator.discovery import DiscoveryResponder, open_responder
from envrec_coordinator.owner import OwnerDesk
from envrec_coordinator.pairing import HELLO_TIMEOUT_S, Decision, PairingBusy, PairingDesk
from envrec_coordinator.registry import Registry
from envrec_coordinator.waiting import WaitingDesk
from envrec_coordinator.web import WEB_HOST, WebScreen
from envrec_net import identity as ident
from envrec_net import pake, tls
from envrec_net.envelope import EnvelopeRejected, Verifier, seal
from envrec_net.log import EventLog
from envrec_net.wire import FrameError, read_frame, write_frame

HEARTBEAT_INTERVAL_S = 10.0
IDLE_TIMEOUT_S = 3 * HEARTBEAT_INTERVAL_S
SUMMARY_EVERY_S = 300.0  # a heartbeat is not an event: the log gets connect, disconnect, a changed card and one summary this often
CAPS = {"hostname": 80, "os": 20, "arch": 20, "os_release": 40, "agent": 40}


class BindRefused(ValueError):
    pass


class WaitingGone(Exception):
    pass


def check_bind(host: str) -> str:
    """The coordinator listens on one address the owner chose: loopback, a private LAN address or a link-local one. Never every interface, never a public address."""
    try:
        a = ipaddress.ip_address(host)
    except ValueError:
        raise BindRefused(f"{host!r} is not an IP address: name the interface address to listen on") from None
    if a.is_unspecified or a.is_multicast or a.is_global:
        raise BindRefused(f"{host} is not allowed: choose a loopback, private or link-local address of this computer")
    return str(a)


def _clip(v, cap: int) -> str:
    return "".join(c for c in str(v) if c.isprintable())[:cap]


class Coordinator:
    def __init__(self, state_dir: Path, host: str, port: int, *, clock=time.time, idle_timeout_s: float = IDLE_TIMEOUT_S, owner_ttl_s: float = 120.0, summary_every_s: float = SUMMARY_EVERY_S,
                 web: bool = False, web_port: int = 0, discovery_port: int | None = None, name: str | None = None, include_virtual: bool = False) -> None:
        self.state, self.host, self.port, self.clock, self.idle_timeout_s = Path(state_dir) / "coordinator", check_bind(host), port, clock, idle_timeout_s
        self.layer = envrec_os.current()
        self.owner_ttl_s = owner_ttl_s
        self._server: asyncio.AbstractServer | None = None
        self._tasks: list[asyncio.Task] = []
        self._conns: set[asyncio.StreamWriter] = set()
        self.summary_every_s = summary_every_s
        self._discovery_port, self.include_virtual = discovery_port, include_virtual
        self.display_name = (name or envrec_os.card()['hostname'])[:60]
        self.responder: DiscoveryResponder | None = None
        self._udp = None
        self._last_disc_summary = 0.0
        self._online: dict[str, int] = {}  # node id -> open authenticated sessions
        self._web_wanted, self._web_port = web, web_port
        self.web: WebScreen | None = None

    async def start(self) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        self.layer.protect_private(self.state)
        self.identity = ident.load_or_create(self.state / "identity", self.layer)
        self.registry = Registry(self.state, self.layer)
        self.log = EventLog(self.state / "log")  # opened on this thread: the store belongs to it
        self.ctx = tls.server_context(self.state / "identity", self.registry.trusted_certs())
        self.verifier = Verifier(self.registry.public_key, clock=self.clock)
        self.watcher = ControlWatcher(self.state, self.layer, self._on_cli_offer)
        self.waiting = WaitingDesk(clock=self.clock, log=lambda t, p: self.log.add(t, p), on_change=self._publish_waiting)
        self.owner = OwnerDesk(self.clock)
        self.desk = PairingDesk(identity=self.identity, registry=self.registry, log=self.log, ask_owner=self._ask_owner, on_paired=lambda der: tls.add_trusted(self.ctx, der),
                                clock=self.clock, owner_ttl_s=self.owner_ttl_s, on_outcome=self._on_outcome, on_stage=lambda i, stage: self.owner.exchanging(i))
        self._server = await asyncio.start_server(self._on_connect, self.host, self.port, ssl=self.ctx)  # THE listening socket
        self.port = self._server.sockets[0].getsockname()[1]
        self._tasks.append(asyncio.create_task(self.watcher.run()))
        if self._discovery_port is not None:
            self.responder = DiscoveryResponder(self.identity, name=self.display_name, tcp_port=self.port, clock=self.clock, include_virtual=self.include_virtual,
                                                log=lambda t, p: self.log.add(t, p))
            self._udp = await open_responder(self.responder, self._discovery_port)  # the ONE discovery socket (optional)
            self._discovery_port = self._udp.get_extra_info("sockname")[1]
            self.log.add("discovery_started", {"udp_port": self._discovery_port})
        self._publish_waiting()
        self._tasks.append(asyncio.create_task(self._tick()))
        self.log.add("coordinator_started", {"host": self.host, "port": self.port, "fingerprint": self.identity.fingerprint, "paired_nodes": len(self.registry.all())})
        if self._web_wanted:
            try:
                self.web = WebScreen(self, port=self._web_port, clock=self.clock)
                self.web.start(asyncio.get_running_loop())
            except OSError:
                self.web = None
                await self.stop()
                raise
            self.log.add("web_started", {"host": WEB_HOST, "port": self.web.port})  # the link carries the one-time secret: it is never logged

    # ---- the owner's front ends: the terminal (control files) and the web screen. Both call these; neither holds protocol logic -------------------

    def _on_cli_offer(self, offer_id: str, code: str, node: str | None = None) -> None:
        try:
            self._on_offer(offer_id, code, "cli", bound_to=node)
        except (WaitingGone, PairingBusy) as exc:
            self.watcher.report(offer_id, "rejected", "that machine is not waiting any more" if isinstance(exc, WaitingGone) else "another pairing is in progress")

    def _on_offer(self, offer_id: str, code: str, via: str = "cli", bound_to: str | None = None) -> None:
        target = None
        if bound_to is not None:
            w = self.waiting.get(bound_to)
            if w is None:
                raise WaitingGone(bound_to)
            if self.desk.busy:
                raise PairingBusy()
            target = w.name
        self.log.add("owner_requested_pairing", {"offer": offer_id, "via": via, **({"waiting": bound_to, "machine": target} if bound_to else {})}, owner=True)
        offer = self.desk.open_offer(offer_id, code, bound_to)
        self.owner.opened(offer_id, code, offer.expires, via, target=target)
        if bound_to is not None:
            self.waiting.pick(bound_to)  # only now may that machine's own connection continue to the key exchange

    def offer_for_waiting(self, wait_id: str, via: str = "web") -> dict:
        """The owner picked a waiting machine: a code is made for that machine only."""
        self.desk.expire_due()
        if self.desk.busy:
            raise PairingBusy()
        self._on_offer(uuid.uuid4().hex[:12], pake.new_code(), via, bound_to=wait_id)
        return self.snapshot()

    def _publish_waiting(self) -> None:
        """The terminal's `mas waiting` reads this file (the coordinator opens no socket for it)."""
        try:
            write_json(self.layer, self.watcher.dir / "waiting.json", {"waiting": [w.view() for w in self.waiting.list()], "at": self.clock()})
        except OSError:
            pass

    def begin_pairing(self, via: str = "web") -> dict:
        """The owner asks for a new code (same effect as `mas pair` on this computer)."""
        self.desk.expire_due()
        if self.desk.busy:
            raise PairingBusy()
        self._on_offer(uuid.uuid4().hex[:12], pake.new_code(), via)
        return self.snapshot()

    def decide_pairing(self, offer_id: str, accept: bool, via: str) -> bool:
        return self.owner.decide(offer_id, accept, via)

    async def _ask_owner(self, summary: dict) -> Decision:
        """Ask the owner in every front end at once; the first answer wins and the other question is withdrawn."""
        cli = asyncio.ensure_future(self.watcher.ask_owner(summary))
        web = asyncio.ensure_future(self.owner.ask(summary))
        try:
            done, _ = await asyncio.wait({cli, web}, return_when=asyncio.FIRST_COMPLETED)
            return web.result() if web in done else Decision(cli.result(), "cli")
        finally:
            for t in (cli, web):
                t.cancel()
            await asyncio.gather(cli, web, return_exceptions=True)

    def _on_outcome(self, offer_id: str, out) -> None:
        self.watcher.report(offer_id, out.status, out.reason)
        self.owner.outcome(offer_id, out.status, out.reason)

    async def _tick(self) -> None:
        while True:
            self.desk.expire_due()  # a code nobody used ends as expired for every front end, visitor or not
            if self.responder is not None and self.clock() - self._last_disc_summary >= self.summary_every_s:
                self._last_disc_summary = self.clock()
                summary = self.responder.summary()
                if any(summary.values()):
                    self.log.add("discovery_summary", summary)
            await asyncio.sleep(1.0)

    def snapshot(self) -> dict:
        """What the owner's screen shows: the paired machines and the pairing in progress."""
        self.desk.expire_due()
        machines = [{"id": n.node_id, "name": n.name, "os": n.os, "arch": n.arch, "status": "online" if self._online.get(n.node_id) else "offline",
                     "last_heartbeat": n.last_heartbeat, "fingerprint": n.fingerprint, "paired_at": n.paired_at} for n in self.registry.all()]
        return {"now": self.clock(), "machines": machines, "pairing": self.owner.view(), "waiting": [w.view() for w in self.waiting.list()]}

    def web_event(self, type: str, payload: dict) -> None:
        self.log.add(type, payload)

    async def stop(self) -> None:
        if self._udp is not None:
            self._udp.close()
        if self.web is not None:
            await asyncio.to_thread(self.web.stop)  # first: its handlers call back into this loop
        for t in self._tasks:
            t.cancel()
        if self._server is not None:
            self._server.close()
            for w in list(self._conns):  # an open node connection would keep `wait_closed` waiting forever
                w.close()
            await self._server.wait_closed()
        self.log.add("coordinator_stopped", {})
        self.log.close()

    # ---- connections ---------------------------------------------------------------------------------------------------------------------------

    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        source = (writer.get_extra_info("peername") or ("?",))[0]
        self._conns.add(writer)
        try:
            der = tls.peer_cert_der(writer)
            if der is None:  # no certificate: a computer that is not paired. The first message says whether it wants to wait for the owner or to try a code.
                try:
                    first = await read_frame(reader, timeout=HELLO_TIMEOUT_S)
                except FrameError:
                    first = {}  # a connection that says nothing valid is refused (and rate-limited) by the pairing desk
                if first.get("type") == "wait_hello":
                    await self.waiting.attend(reader, writer, source, first, pair=self.desk.handle, cert_der=self.identity.cert_der, read_frame=read_frame)
                else:
                    await self.desk.handle(reader, writer, source, self.identity.cert_der, first_frame=first)
            else:
                await self._node_session(reader, writer, der, source)
        except Exception as exc:  # one bad connection never takes the server down
            self.log.add("connection_error", {"source": source, "error": type(exc).__name__})
        finally:
            self._conns.discard(writer)
            writer.close()

    async def _node_session(self, reader, writer, cert_der: bytes, source: str) -> None:
        rec = self.registry.by_cert(cert_der)
        if rec is None:  # the handshake should have refused it; if it did not, so does this
            self.log.add("connection_refused", {"source": source, "reason": "unpaired_certificate"})
            return
        self.log.add("node_connected", {"node": rec.node_id, "source": source})
        self._online[rec.node_id] = self._online.get(rec.node_id, 0) + 1
        reason = "closed by the node"
        beats, last_summary, last_card = 0, self.clock(), None
        try:
            while True:
                try:
                    raw = await read_frame(reader, timeout=self.idle_timeout_s)
                except FrameError as exc:
                    reason = str(exc)[:60]
                    break
                try:
                    env = self.verifier.open(raw, expect_sender=rec.node_id)
                except EnvelopeRejected as exc:
                    self.log.add("message_rejected", {"node": rec.node_id, "reason": exc.reason, "source": source})
                    reason = f"message rejected: {exc.reason}"
                    break
                if env.type != "heartbeat":
                    self.log.add("message_rejected", {"node": rec.node_id, "reason": "unknown_type", "type": _clip(env.type, 30), "source": source})
                    reason = "message rejected: unknown_type"
                    break
                card = {k: _clip(env.payload.get(k, ""), cap) for k, cap in CAPS.items()}
                self.registry.heartbeat(rec.node_id, self.clock())
                beats += 1
                if card != last_card:
                    last_card = card
                    self.log.add("node_card", {"node": rec.node_id, **card})
                if self.clock() - last_summary >= self.summary_every_s:
                    self.log.add("heartbeat_summary", {"node": rec.node_id, "beats": beats, "seconds": round(self.clock() - last_summary)})
                    beats, last_summary = 0, self.clock()
                await write_frame(writer, seal(self.identity, "ack", {"nonce": env.nonce, "interval_s": HEARTBEAT_INTERVAL_S}, clock=self.clock))
        finally:
            if beats:
                self.log.add("heartbeat_summary", {"node": rec.node_id, "beats": beats, "seconds": round(self.clock() - last_summary)})
            self._online[rec.node_id] -= 1
            if not self._online[rec.node_id]:
                del self._online[rec.node_id]
            self.log.add("node_disconnected", {"node": rec.node_id, "reason": reason})

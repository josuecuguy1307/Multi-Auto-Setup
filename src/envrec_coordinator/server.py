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
from envrec_coordinator.control import ControlWatcher
from envrec_coordinator.owner import OwnerDesk
from envrec_coordinator.pairing import Decision, PairingBusy, PairingDesk
from envrec_coordinator.registry import Registry
from envrec_coordinator.web import WEB_HOST, WebScreen
from envrec_net import identity as ident
from envrec_net import pake, tls
from envrec_net.envelope import EnvelopeRejected, Verifier, seal
from envrec_net.log import EventLog
from envrec_net.wire import FrameError, read_frame, write_frame

HEARTBEAT_INTERVAL_S = 10.0
IDLE_TIMEOUT_S = 3 * HEARTBEAT_INTERVAL_S
CAPS = {"hostname": 80, "os": 20, "arch": 20, "os_release": 40, "agent": 40}


class BindRefused(ValueError):
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
    def __init__(self, state_dir: Path, host: str, port: int, *, clock=time.time, idle_timeout_s: float = IDLE_TIMEOUT_S, owner_ttl_s: float = 120.0,
                 web: bool = False, web_port: int = 0) -> None:
        self.state, self.host, self.port, self.clock, self.idle_timeout_s = Path(state_dir) / "coordinator", check_bind(host), port, clock, idle_timeout_s
        self.layer = envrec_os.current()
        self.owner_ttl_s = owner_ttl_s
        self._server: asyncio.AbstractServer | None = None
        self._tasks: list[asyncio.Task] = []
        self._conns: set[asyncio.StreamWriter] = set()
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
        self.watcher = ControlWatcher(self.state, self.layer, self._on_offer)
        self.owner = OwnerDesk(self.clock)
        self.desk = PairingDesk(identity=self.identity, registry=self.registry, log=self.log, ask_owner=self._ask_owner, on_paired=lambda der: tls.add_trusted(self.ctx, der),
                                clock=self.clock, owner_ttl_s=self.owner_ttl_s, on_outcome=self._on_outcome, on_stage=lambda i, stage: self.owner.exchanging(i))
        self._server = await asyncio.start_server(self._on_connect, self.host, self.port, ssl=self.ctx)  # THE listening socket
        self.port = self._server.sockets[0].getsockname()[1]
        self._tasks.append(asyncio.create_task(self.watcher.run()))
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

    def _on_offer(self, offer_id: str, code: str, via: str = "cli") -> None:
        self.log.add("owner_requested_pairing", {"offer": offer_id, "via": via}, owner=True)
        offer = self.desk.open_offer(offer_id, code)
        self.owner.opened(offer_id, code, offer.expires, via)

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
            await asyncio.sleep(1.0)

    def snapshot(self) -> dict:
        """What the owner's screen shows: the paired machines and the pairing in progress."""
        self.desk.expire_due()
        machines = [{"id": n.node_id, "name": n.name, "os": n.os, "arch": n.arch, "status": "online" if self._online.get(n.node_id) else "offline",
                     "last_heartbeat": n.last_heartbeat, "fingerprint": n.fingerprint, "paired_at": n.paired_at} for n in self.registry.all()]
        return {"now": self.clock(), "machines": machines, "pairing": self.owner.view()}

    def web_event(self, type: str, payload: dict) -> None:
        self.log.add(type, payload)

    async def stop(self) -> None:
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
            if der is None:
                await self.desk.handle(reader, writer, source, self.identity.cert_der)
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
                self.log.add("heartbeat", {"node": rec.node_id, **card, "seq": env.payload.get("seq") if isinstance(env.payload.get("seq"), int) else None})
                await write_frame(writer, seal(self.identity, "ack", {"nonce": env.nonce, "interval_s": HEARTBEAT_INTERVAL_S}, clock=self.clock))
        finally:
            self._online[rec.node_id] -= 1
            if not self._online[rec.node_id]:
                del self._online[rec.node_id]
            self.log.add("node_disconnected", {"node": rec.node_id, "reason": reason})

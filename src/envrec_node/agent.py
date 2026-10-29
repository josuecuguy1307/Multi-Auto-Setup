"""The node agent: dials out to the paired coordinator over the pinned channel, sends a signed heartbeat on a timer and checks the signed acknowledgement, and when the connection is
lost waits with growing delays and dials again. It opens no listening socket and executes nothing. Its connect/heartbeat flow is `protocol-node-connect`, enforced by the guard."""

from __future__ import annotations

import asyncio
import random
import ssl
import time
from collections.abc import Callable, Iterator

import envrec_os
from envrec_net import tls
from envrec_net.envelope import EnvelopeRejected, Verifier, seal
from envrec_net.log import EventLog
from envrec_net.protocol import IllegalTransition, ProtocolGuard
from envrec_net.wire import FrameError, read_frame, write_frame
from envrec_node.discover import DiscoveryFailed, discover
from envrec_node.state import NodeState, Pin

AGENT = "mas-node 0.1"
CONNECT_TIMEOUT_S = 10.0


def backoff_delays(base: float = 1.0, cap: float = 60.0, jitter: Callable[[], float] = random.random) -> Iterator[float]:
    """1, 2, 4, ... up to the cap, each stretched by up to 25% so many nodes do not retry in step."""
    d = base
    while True:
        yield min(cap, d) * (1 + 0.25 * jitter())
        d = min(cap, d * 2)


class NodeAgent:
    def __init__(self, state_dir, *, interval_s: float = 10.0, clock=time.time, sleep=asyncio.sleep, jitter: Callable[[], float] = random.random, base_delay_s: float = 1.0,
                 rediscover: bool = False, discovery_port: int = 7444, discovery_targets: list | None = None) -> None:
        self.state = NodeState(state_dir)
        self.identity = self.state.identity()
        self.pin: Pin | None = self.state.pin()
        self.interval_s, self.clock, self.sleep, self.jitter, self.base_delay_s = interval_s, clock, sleep, jitter, base_delay_s
        self.log = EventLog(self.state.log_dir)
        self.guard = ProtocolGuard("protocol-node-connect", lambda t, p: self.log.add(t, p), clock=clock, subject=self.identity.node_id)
        self.rediscover, self.discovery_port, self.discovery_targets = rediscover, discovery_port, discovery_targets
        self.connected = False
        self.heartbeats_acked = 0
        self._acked_this_session = 0
        self.stop_event = asyncio.Event()
        self._kick = asyncio.Event()
        self._seq = 0
        self._loop: asyncio.AbstractEventLoop | None = None

    def card(self) -> dict:
        c = envrec_os.card()
        return {"hostname": c["hostname"], "os": c["os"], "os_release": c["os_release"], "arch": c["arch"], "agent": AGENT}

    def _step(self, to: str) -> None:
        try:
            self.guard.advance(to)
        except IllegalTransition:
            pass  # refused and logged by the guard; the agent keeps its own course

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        if self.pin is None:
            raise RuntimeError("this node is not paired: run `mas pair HOST:PORT` first")
        self.log.add("node_started", {"node": self.identity.node_id, "coordinator": self.pin.address})
        self.guard.record("node.hostname", self.card()["hostname"])
        self._step("connect")
        delays = backoff_delays(self.base_delay_s, jitter=self.jitter)
        try:
            while not self.stop_event.is_set():
                reason = await self._one_session()
                if self.stop_event.is_set():
                    break
                self.guard.record("net.connected", False)
                self._step("backoff")
                delay = next(delays)
                self.log.add("reconnect_wait", {"reason": reason[:80], "delay_s": round(delay, 2)})
                try:
                    await asyncio.wait_for(self.stop_event.wait(), delay)
                except TimeoutError:
                    pass
                if self.rediscover and reason.startswith("connect failed"):
                    await self._find_coordinator_again()
                self.guard.record("net.retry_due", True)
                self._step("connect")
                if self.heartbeats_acked_in_last_session:
                    delays = backoff_delays(self.base_delay_s, jitter=self.jitter)
        finally:
            self.guard.record("node.stop", True)
            self._step("stopped")
            self.log.add("node_stopped", {})
            self.log.close()

    heartbeats_acked_in_last_session = False

    async def _find_coordinator_again(self) -> None:
        """The coordinator may have a new address. Ask the network; accept only an answer signed by the key pinned at pairing (an unsigned or differently signed answer is ignored)."""
        assert self.pin is not None
        try:
            answers = await asyncio.to_thread(discover, self.discovery_port, 2.0, pinned=(self.pin.coordinator_id, self.pin.public_raw), clock=self.clock, targets=self.discovery_targets)
        except DiscoveryFailed as exc:
            self.log.add("rediscovery_failed", {"reason": exc.code})
            return
        for a in answers:
            if a.signed and a.coordinator_id == self.pin.coordinator_id:
                new = f"{a.address}:{a.port}"
                if new != self.pin.address:
                    old = self.pin.address
                    self.pin = self.state.save_pin(new, self.pin.cert_der, clock=lambda: self.pin.paired_at)  # type: ignore[union-attr]
                    self.log.add("coordinator_moved", {"from": old, "to": new})
                return

    async def _one_session(self) -> str:
        assert self.pin is not None
        host, port = self.pin.host_port
        self.heartbeats_acked_in_last_session = False
        self._acked_this_session = 0
        try:
            ctx = tls.client_context(self.state.identity_dir, self.pin.cert_der)
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port, ssl=ctx), CONNECT_TIMEOUT_S)
        except (OSError, TimeoutError, ssl.SSLError) as exc:
            self.log.add("connect_failed", {"error": type(exc).__name__})
            return f"connect failed: {type(exc).__name__}"
        verifier = Verifier(lambda sid: self.pin.public_raw if sid == self.pin.coordinator_id else None, clock=self.clock)  # type: ignore[union-attr]
        self.connected = True
        self.guard.record("net.connected", True)
        self._step("online")
        self.log.add("connected", {"coordinator": self.pin.coordinator_id})
        reason = "stopped"
        try:
            while not self.stop_event.is_set():
                ok, reason = await self._heartbeat(reader, writer, verifier)
                if not ok:
                    break
                await self._wait_for_next_beat()
        finally:
            self.connected = False
            writer.close()
            self.log.add("disconnected", {"reason": reason[:80], "heartbeats_acked": self._acked_this_session})  # one count per session, not one event per heartbeat
        return reason

    async def _wait_for_next_beat(self) -> None:
        """Until the interval is over, the agent is stopped, or the typed tool asks for a heartbeat now."""
        waiters = {asyncio.ensure_future(self.stop_event.wait()), asyncio.ensure_future(self._kick.wait())}
        _, pending = await asyncio.wait(waiters, timeout=self.interval_s, return_when=asyncio.FIRST_COMPLETED)
        for w in pending:
            w.cancel()
        self._kick.clear()

    async def _heartbeat(self, reader, writer, verifier: Verifier) -> tuple[bool, str]:
        self._seq += 1
        payload = {**self.card(), "seq": self._seq}
        try:
            await write_frame(writer, seal(self.identity, "heartbeat", payload, clock=self.clock))
            raw = await read_frame(reader, timeout=CONNECT_TIMEOUT_S)
            env = verifier.open(raw, expect_sender=self.pin.coordinator_id)  # type: ignore[union-attr]
        except (FrameError, ConnectionError, OSError) as exc:
            return False, f"connection lost: {str(exc)[:50]}"
        except EnvelopeRejected as exc:
            self.log.add("message_rejected", {"reason": exc.reason})
            return False, f"coordinator message rejected: {exc.reason}"
        if env.type != "ack" or env.payload.get("nonce") is None:
            self.log.add("message_rejected", {"reason": "unexpected_type"})
            return False, "unexpected message from the coordinator"
        self.heartbeats_acked += 1
        self.heartbeats_acked_in_last_session = True
        self._acked_this_session += 1
        return True, ""

    # ---- for the typed tool `node_heartbeat_send`, called from another thread ------------------------------------------------------------------------------

    def status(self) -> dict:
        return {"connected": self.connected, "heartbeats_acked": self.heartbeats_acked, "seq": self._seq}

    def heartbeat_now(self, timeout_s: float = 10.0) -> dict:
        """Ask the running agent for a heartbeat now and wait for the coordinator's signed acknowledgement. Called from a tool's worker thread, never from the agent's own loop."""
        if self._loop is None or not self.connected:
            return {"acked": False, "reason": "not connected", **self.status()}
        before = self.heartbeats_acked
        self._loop.call_soon_threadsafe(self._kick.set)
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            if self.heartbeats_acked > before:
                return {"acked": True, **self.status()}
            time.sleep(0.05)
        return {"acked": False, "reason": "no acknowledgement in time", **self.status()}

    def stop(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self.stop_event.set)

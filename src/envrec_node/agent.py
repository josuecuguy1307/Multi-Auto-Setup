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
from envrec_net.envelope import EnvelopeRejected
from envrec_net.session import PLAIN, PROTOCOL, Sealer, SessionVerifier
from envrec_net.task import TokenChecker
from envrec_net.log import EventLog
from envrec_net.protocol import IllegalTransition, ProtocolGuard
from envrec_net.wire import FrameError, read_frame, write_frame
from envrec_node.discover import DiscoveryFailed, discover
from envrec_node.state import NodeState, Pin
from envrec_node.tasks import TaskRunner
from envrec_net.identity import KEY_FILE

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
                 rediscover: bool = False, discovery_port: int = 7444, discovery_targets: list | None = None, on_message: Callable[[str], None] = lambda text: None,
                 skew_warn_s: float = 300.0) -> None:
        self.state = NodeState(state_dir)
        self.identity = self.state.identity()
        self.pin: Pin | None = self.state.pin()
        self.interval_s, self.clock, self.sleep, self.jitter, self.base_delay_s = interval_s, clock, sleep, jitter, base_delay_s
        self.log = EventLog(self.state.log_dir)
        self.guard = ProtocolGuard("protocol-node-connect", lambda t, p: self.log.add(t, p), clock=clock, subject=self.identity.node_id)
        self.rediscover, self.discovery_port, self.discovery_targets = rediscover, discovery_port, discovery_targets
        self.on_message, self.skew_warn_s = on_message, skew_warn_s
        self.clock_offset_s = 0.0
        self.skew_s = None
        self._wlock = asyncio.Lock()
        self.runner = TaskRunner(key_path=self.state.identity_dir / KEY_FILE, agent=AGENT)
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
                self.say(f"Trying again in {delay:.0f} s.")
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

    def say(self, text: str) -> None:
        """Tell the owner something in plain words (the terminal prints it); nothing the node does is silent."""
        self.on_message(text)

    def _describe_connect_error(self, exc: Exception, address: str) -> str:
        if isinstance(exc, ssl.SSLCertVerificationError):
            return (f"The computer at {address} is not the coordinator this node paired with (its certificate is not the pinned one). Nothing was sent. If the coordinator's state was reset, "
                    "pair again with `mas join`.")
        if isinstance(exc, ssl.SSLError):
            return (f"The coordinator at {address} closed the secure connection during setup ({exc.reason or 'TLS error'}). The most likely cause is that it does not know this computer "
                    "(not paired, or unpaired): run `mas join` again.")
        if isinstance(exc, TimeoutError):
            return f"No answer from {address} in {CONNECT_TIMEOUT_S:.0f} s. Is `mas coordinator start` running there, and are both computers on the same network? A firewall may block the port."
        return f"Could not connect to {address} ({type(exc).__name__}). Is the coordinator running, and is the address still right? This node will look for it again."

    async def _send(self, writer, sealer, type: str, payload: dict) -> None:
        async with self._wlock:
            await write_frame(writer, sealer.seal(type, payload))

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
            self.say(self._describe_connect_error(exc, self.pin.address))
            return f"connect failed: {type(exc).__name__}"
        verifier = SessionVerifier(self.pin.coordinator_id, self.pin.public_raw)
        try:
            raw = await read_frame(reader, timeout=CONNECT_TIMEOUT_S)
            env = verifier.open(raw, first=True)
        except FrameError as exc:
            writer.close()
            self.say(f"The coordinator closed the connection before it said anything ({exc}).")
            return f"connection lost: {str(exc)[:50]}"
        except EnvelopeRejected as exc:
            writer.close()
            reason = f"coordinator message rejected: {exc.reason}"
            self.say(f"The coordinator's first message was refused ({PLAIN.get(exc.reason, exc.reason)}).")
            return reason
        if env.type == "reject":
            writer.close()
            return self._refused(env.payload)
        if env.type != "challenge" or env.payload.get("proto") != PROTOCOL:
            writer.close()
            self.say("The coordinator speaks a different version of the connection protocol. Update this program and the coordinator to the same release.")
            return "unexpected message from the coordinator"
        sid = str(env.payload["nonce"])
        sealer = Sealer(self.identity, sid)
        server_time = env.payload.get("server_time")
        if isinstance(server_time, (int, float)) and not isinstance(server_time, bool):
            self.clock_offset_s = float(server_time) - self.clock()  # the coordinator's clock is the reference: used for log times, never to refuse anything
            self.log.add("clock_offset", {"offset_s": round(self.clock_offset_s, 1)})
            if abs(self.clock_offset_s) > self.skew_warn_s:
                self.say(f"The clock of this computer differs from the coordinator's by about {abs(self.clock_offset_s) / 60:.0f} min. The connection works; you may want to set the clock automatically.")
        self.runner.new_session(TokenChecker(self.pin.public_raw, self.identity.node_id, sid))
        try:
            await self._send(writer, sealer, "hello", {"nonce": sid, "proto": PROTOCOL, "local_time": self.clock(), **self.card()})
        except (ConnectionError, OSError) as exc:
            writer.close()
            return f"connection lost: {str(exc)[:50]}"
        self.connected = True
        self.guard.record("net.connected", True)
        self._step("online")
        self.log.add("connected", {"coordinator": self.pin.coordinator_id})
        reader_task = asyncio.ensure_future(self._read_loop(reader, writer, verifier, sealer))
        reason = "stopped"
        try:
            while not self.stop_event.is_set() and not reader_task.done():
                self._seq += 1
                try:
                    await self._send(writer, sealer, "heartbeat", {**self.card(), "seq": self._seq})
                except (ConnectionError, OSError) as exc:
                    reason = f"connection lost: {str(exc)[:50]}"
                    break
                await self._wait_for_next_beat(reader_task)
            if reader_task.done() and not reader_task.cancelled():
                reason = reader_task.result()
        finally:
            reader_task.cancel()
            self.connected = False
            writer.close()
            self.log.add("disconnected", {"reason": reason[:80], "heartbeats_acked": self._acked_this_session})  # one count per session, not one event per heartbeat
        return reason

    def _refused(self, payload: dict) -> str:
        code = str(payload.get("reason", ""))[:40]
        self.say(f"The coordinator refused this connection: {PLAIN.get(code, code)}")
        self.log.add("refused_by_coordinator", {"reason": code})
        return f"coordinator refused: {code}"

    async def _read_loop(self, reader, writer, verifier: SessionVerifier, sealer: Sealer) -> str:
        """Everything the coordinator sends after the challenge: welcome, acknowledgements, tasks, cancellations, and a typed refusal."""
        send = lambda t, p: self._send(writer, sealer, t, p)  # noqa: E731
        while not self.stop_event.is_set():
            try:
                raw = await read_frame(reader, timeout=max(self.interval_s * 4, CONNECT_TIMEOUT_S))
                env = verifier.open(raw)
            except FrameError as exc:
                return f"connection lost: {str(exc)[:50]}"
            except EnvelopeRejected as exc:
                self.log.add("message_rejected", {"reason": exc.reason})
                self.say(f"A message from the coordinator was refused ({PLAIN.get(exc.reason, exc.reason)}); the connection was closed.")
                return f"coordinator message rejected: {exc.reason}"
            if env.type == "ack" and env.payload.get("nonce") is not None:
                self.heartbeats_acked += 1
                self._acked_this_session += 1
                self.heartbeats_acked_in_last_session = True
            elif env.type == "welcome":
                self.skew_s = env.payload.get("skew_s")
            elif env.type == "task":
                await self.runner.on_task(env.payload, send)
            elif env.type == "cancel":
                await self.runner.on_cancel(env.payload, send)
            elif env.type == "reject":
                return self._refused(env.payload)
            else:
                self.log.add("message_rejected", {"reason": "unexpected_type"})
                return "unexpected message from the coordinator"
        return "stopped"

    async def _wait_for_next_beat(self, reader_task) -> None:
        """Until the interval is over, the agent is stopped, the typed tool asks for a heartbeat now, or the connection ended."""
        waiters = {asyncio.ensure_future(self.stop_event.wait()), asyncio.ensure_future(self._kick.wait()), reader_task}
        _, pending = await asyncio.wait(waiters, timeout=self.interval_s, return_when=asyncio.FIRST_COMPLETED)
        for w in pending:
            if w is not reader_task:
                w.cancel()
        self._kick.clear()

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

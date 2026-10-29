"""A node joining a coordinator without typing an address (docs/NETWORK_CONTRACT.md 1.8). Flow, in order: discover coordinators on the LAN, show what was found, the OWNER picks one, the node dials
that coordinator WITHOUT a certificate and registers as waiting (name, operating system, architecture, the fingerprint it claims: nothing else), then waits until the owner presses
"pair" for this machine on the coordinator. Only then is there a code: the owner types it here, once, and the ordinary key exchange runs on the same connection."""

from __future__ import annotations

import asyncio
import base64
import ssl
from collections.abc import Awaitable, Callable
from pathlib import Path

import envrec_os
from envrec_net import discovery, tls
from envrec_net.identity import fingerprint_of
from envrec_net.protocol import IllegalTransition, ProtocolGuard
from envrec_net.wire import FrameError, read_frame, write_frame
from envrec_node.pair import CONNECT_TIMEOUT_S, PairingFailed, exchange
from envrec_node.state import NodeState, Pin

WAIT_VERSION = 1
WAIT_MESSAGES = {
    "waiting_full": "The coordinator already has as many waiting computers as it accepts. Try again in a few minutes.",
    "rate_limited": "Too many attempts from this computer in a short time. Wait a minute and try again.",
    "bad_hello": "The coordinator did not understand this computer's first message.",
    "wait_expired": "The wait ended before the owner paired this computer. Run `mas join` again.",
}


async def join(chosen: discovery.Answer | tuple[str, int], state_dir: Path, *, name: str | None = None, ask_code: Callable[[], Awaitable[str]], on_waiting: Callable[[str], None] = lambda m: None,
               on_event: Callable[[str, dict], None] = lambda t, p: None, wait_s: float = 660.0) -> Pin:
    """Register as waiting on `chosen` and pair once the owner picks this machine. `ask_code` is how the owner types the 6 digits (a prompt in the CLI, a stub in tests)."""
    host, port = (chosen.address, chosen.port) if isinstance(chosen, discovery.Answer) else chosen
    address = f"{host}:{port}"
    state = NodeState(state_dir)
    me = state.identity()
    card = envrec_os.card()
    label = (name or card["hostname"])[:80]
    identity_claim = {"name": label, "os": card["os"], "arch": card["arch"], "cert_der_b64": base64.b64encode(me.cert_der).decode()}
    guard = ProtocolGuard("protocol-discover", lambda t, p: on_event(t, p), subject=address)

    def step(to: str, **facts) -> None:
        for k, v in facts.items():
            guard.record(k, v)
        try:
            guard.advance(to)
        except IllegalTransition:
            pass

    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port, ssl=tls.pairing_client_context()), CONNECT_TIMEOUT_S)
    except (OSError, TimeoutError, ssl.SSLError) as exc:
        raise PairingFailed("connection_failed", f"Could not reach the coordinator at {address} ({type(exc).__name__}).") from None
    try:
        await write_frame(writer, {"type": "wait_hello", "version": WAIT_VERSION, "name": label, "os": card["os"], "arch": card["arch"], "fingerprint": fingerprint_of(me.cert_der)})
        try:
            ack = await read_frame(reader, timeout=CONNECT_TIMEOUT_S)
        except FrameError as exc:
            raise PairingFailed("connection_failed", f"The coordinator closed the connection ({exc}).") from None
        if ack.get("type") == "wait_error":
            r = str(ack.get("reason", ""))[:40]
            raise PairingFailed(r or "refused", WAIT_MESSAGES.get(r, f"The coordinator refused ({r})."))
        if ack.get("type") != "wait_ack":
            raise PairingFailed("bad_hello", WAIT_MESSAGES["bad_hello"])
        step("listed", **{"discover.answers": True})  # the caller discovered and showed the coordinators, and the OWNER picked this one before we were called
        step("picked", **{"discover.owner_pick": True})
        step("waiting", **{"wait.registered": True})
        on_waiting(f"Waiting for the owner to pair this computer on the coordinator {address}. On the coordinator, choose this computer ({label}) in the list of waiting computers.")
        try:
            msg = await read_frame(reader, timeout=wait_s)
        except FrameError:
            raise PairingFailed("wait_expired", WAIT_MESSAGES["wait_expired"]) from None
        if msg.get("type") == "wait_error":
            r = str(msg.get("reason", ""))[:40]
            raise PairingFailed(r or "refused", WAIT_MESSAGES.get(r, f"The coordinator refused ({r})."))
        if msg.get("type") != "pair_ready":
            raise PairingFailed("bad_hello", WAIT_MESSAGES["bad_hello"])
        step("ready", **{"wait.pair_ready": True})
        code = (await ask_code()).strip()
        if not (code.isdigit() and len(code) == 6):
            raise PairingFailed("bad_code", "The code is 6 digits.")
        return await exchange(reader, writer, code, state, me, identity_claim, address)
    finally:
        writer.close()

"""The node's side of pairing (protocol-pair). The owner types the coordinator's address and the code once. The code is the password of the key exchange and goes nowhere else; the
node learns the coordinator's certificate from the TLS channel and, only if the coordinator proves it knows the same key AND saw the same certificate, pins it."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ssl
from pathlib import Path

import envrec_os
from envrec_net import pake, tls
from envrec_net.identity import Identity, fingerprint_of, node_id_of, public_raw_of_cert
from envrec_net.wire import FrameError, read_frame, write_frame
from envrec_node.state import NodeState, Pin

VERSION = 1
CONNECT_TIMEOUT_S = 10.0
REPLY_TIMEOUT_S = 15.0
RESULT_TIMEOUT_S = 150.0  # the owner has two minutes to answer on the coordinator


class PairingFailed(Exception):
    """`code` is a short typed reason; the message is what the owner is told."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


MESSAGES = {
    "no_active_code": "The coordinator has no code waiting. Run `mas pair` on the coordinator again and use the new code within two minutes.",
    "rate_limited": "Too many attempts from this computer in a short time. Wait a minute and ask the coordinator for a new code.",
    "pairing_in_progress": "The coordinator is already pairing another computer. Try again in a moment.",
    "bad_hello": "The coordinator did not understand the first message.",
    "confirmation_invalid": "The code was not accepted: it is wrong, it was already used, or something is between the two computers. Nothing was saved. Ask for a new code.",
    "owner_timeout": "The owner did not answer on the coordinator in time. Nothing was saved.",
}


async def pair(address: str, code: str, state_dir: Path, *, name: str | None = None) -> Pin:
    host, _, port = address.rpartition(":")
    if not host or not port.isdigit():
        raise PairingFailed("bad_address", "The coordinator address looks like HOST:PORT.")
    if not (code.isdigit() and len(code) == 6):
        raise PairingFailed("bad_code", "The code is 6 digits.")
    state = NodeState(state_dir)
    me: Identity = state.identity()
    card = envrec_os.card()
    node_identity = {"name": (name or card["hostname"])[:80], "os": card["os"], "arch": card["arch"], "cert_der_b64": base64.b64encode(me.cert_der).decode()}
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host.strip("[]"), int(port), ssl=tls.pairing_client_context()), CONNECT_TIMEOUT_S)
    except (OSError, TimeoutError, ssl.SSLError) as exc:
        raise PairingFailed("connection_failed", f"Could not reach the coordinator at {address} ({type(exc).__name__}). Check the address and that `mas coordinator start` is running.") from None
    try:
        seen = tls.peer_cert_der(writer)
        if seen is None:
            raise PairingFailed("connection_failed", "The coordinator did not present a certificate.")
        side = pake.Side("node", code)
        await write_frame(writer, {"type": "pair_hello", "version": VERSION, "msg_a": side.message.hex()})
        try:
            reply = await read_frame(reader, timeout=REPLY_TIMEOUT_S)
        except FrameError as exc:
            raise PairingFailed("connection_failed", f"The coordinator closed the connection ({exc}).") from None
        if reply.get("type") == "pair_error":
            r = str(reply.get("reason", ""))[:40]
            raise PairingFailed(r or "refused", MESSAGES.get(r, f"The coordinator refused ({r})."))
        try:
            msg_b, their_confirm = bytes.fromhex(str(reply["msg_b"])), bytes.fromhex(str(reply["confirm"]))
            side.finish(msg_b)
        except (KeyError, ValueError, pake.PakeError):
            raise PairingFailed("confirmation_invalid", MESSAGES["confirmation_invalid"]) from None
        assert side.key is not None
        tr = pake.transcript(side.message, msg_b, seen)
        k_coord, k_node = pake.confirmation_keys(side.key, tr)
        coordinator = {"id": node_id_of(public_raw_of_cert(seen)), "cert_sha256": hashlib.sha256(seen).hexdigest()}  # what THIS node saw on the channel
        if not pake.equal(their_confirm, pake.coordinator_confirm(k_coord, tr, coordinator)):
            raise PairingFailed("confirmation_invalid", MESSAGES["confirmation_invalid"])
        await write_frame(writer, {"type": "pair_confirm", "version": VERSION, "confirm": pake.node_confirm(k_node, tr, node_identity).hex(), "node": node_identity})
        print(f"Waiting for the owner to confirm this computer on the coordinator. This computer's fingerprint: {fingerprint_of(me.cert_der)}", flush=True)
        try:
            result = await read_frame(reader, timeout=RESULT_TIMEOUT_S)
        except FrameError:
            raise PairingFailed("owner_timeout", MESSAGES["owner_timeout"]) from None
        if result.get("type") == "pair_error":
            r = str(result.get("reason", ""))[:40]
            raise PairingFailed(r or "refused", MESSAGES.get(r, f"The coordinator refused ({r})."))
        accepted = result.get("accepted") is True
        try:
            ok_mac = pake.equal(bytes.fromhex(str(result["mac"])), pake.result_mac(k_coord, tr, accepted))
        except (KeyError, ValueError):
            ok_mac = False
        if result.get("type") != "pair_result" or not ok_mac:
            raise PairingFailed("confirmation_invalid", "The coordinator's final answer could not be verified. Nothing was saved.")
        if not accepted:
            raise PairingFailed("refused_by_owner", "The owner did not accept this computer on the coordinator. Nothing was saved.")
        return state.save_pin(address, seen)
    finally:
        writer.close()

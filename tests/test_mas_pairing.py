"""Pairing (protocol-pair) and the attacks on it. Each attack must fail closed AND leave its reason in the event log. SIMULATED-LOCAL: real TLS on loopback, one machine."""
from __future__ import annotations

import asyncio
import json
import ssl

import pytest

import envrec_os
from envrec_net import tls
from envrec_net.identity import Identity, create
from envrec_net.protocol import IllegalTransition, ProtocolGuard
from envrec_net.wire import FrameError, read_frame, write_frame
from envrec_node.pair import PairingFailed
from envrec_node.state import NodeState
from mas_world import Clock, coordinator, events, offer, pair_node, wait_event

CODE = "482915"


def run(coro):
    return asyncio.run(coro)


def types(c):
    return [e.type for e in events(c)]


# ---- the happy path ---------------------------------------------------------------------------------------------------------------------------

def test_a_node_pairs_with_the_right_code_after_the_owner_says_yes_and_both_sides_pin_each_other(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            offer(c, CODE)
            pin = await pair_node(c, tmp_path, CODE)
            assert pin.cert_der == c.identity.cert_der  # the node pinned exactly the coordinator's certificate
            nodes = c.registry.all()
            assert len(nodes) == 1 and nodes[0].name == "node1" and nodes[0].cert_der == NodeState(tmp_path / "node1").identity().cert_der  # ...and the coordinator the node's
            assert c.asked and set(c.asked[0]) >= {"name", "os", "arch", "fingerprint"}  # the owner was shown who is asking
            ev = types(c)
            assert ev.index("pair_offer_created") < ev.index("pair_session_started") < ev.index("owner_decision") < ev.index("pair_accepted")
            owner = [e for e in events(c, "owner_decision")]
            assert owner[0].effective_tag == "OWNER" and owner[0].payload["accepted"] is True  # the yes is the owner's act
            assert all(e.effective_tag in ("GUARD", "OWNER") for e in events(c))  # no SCRIPT, no MODEL, nothing booked to anyone else
            states = [(e.payload["from"], e.payload["to"]) for e in events(c, "protocol_state")]
            assert states == [("offered", "exchange"), ("exchange", "owner_confirm"), ("owner_confirm", "accepted")]
    run(go())


def test_the_code_is_in_no_event_no_file_and_no_message_on_disk(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            offer(c, CODE)
            await pair_node(c, tmp_path, CODE)
            blobs = [json.dumps(e.payload) for e in events(c)]
            for p in list((tmp_path / "coord").rglob("*")) + list((tmp_path / "node1").rglob("*")):
                if p.is_file() and p.suffix in (".json", ".pem", ".db"):
                    blobs.append(p.read_bytes().decode("latin-1"))
            assert not any(CODE in b for b in blobs)
            layer = envrec_os.current()
            assert layer.is_private(tmp_path / "coord" / "coordinator" / "identity" / "key.pem") and layer.is_private(tmp_path / "node1" / "node" / "identity" / "key.pem")  # private keys: owner-only
    run(go())


# ---- attacks ---------------------------------------------------------------------------------------------------------------------------------

def test_a_wrong_code_fails_stores_nothing_logs_why_and_kills_the_code(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            offer(c, CODE)
            with pytest.raises(PairingFailed) as exc:
                await pair_node(c, tmp_path, "000000")
            assert exc.value.code == "confirmation_invalid"
            assert c.registry.all() == [] and NodeState(tmp_path / "node1").pin() is None
            assert not c.asked  # the owner was never even bothered
            failed = await wait_event(c, "pair_failed")
            assert failed and "wrong code" in failed[0].payload["reason"]
            with pytest.raises(PairingFailed) as again:  # the RIGHT code now: the offer is dead after one failed attempt
                await pair_node(c, tmp_path, CODE)
            assert again.value.code == "no_active_code"
            assert any(e.payload.get("reason") == "no_active_code" for e in events(c, "pair_refused"))
    run(go())


def test_a_second_attempt_with_the_same_code_is_refused_even_when_the_first_succeeded(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            offer(c, CODE)
            await pair_node(c, tmp_path, CODE, node="node1")
            with pytest.raises(PairingFailed) as exc:
                await pair_node(c, tmp_path, CODE, node="node2")
            assert exc.value.code == "no_active_code" and len(c.registry.all()) == 1
    run(go())


def test_a_code_older_than_two_minutes_is_refused_and_logged_as_expired(tmp_path):
    clock = Clock()

    async def go():
        async with coordinator(tmp_path, clock=clock) as c:
            offer(c, CODE)
            clock.advance(121)  # injectable clock: no waiting
            with pytest.raises(PairingFailed) as exc:
                await pair_node(c, tmp_path, CODE)
            assert exc.value.code == "no_active_code" and c.registry.all() == []
            assert await wait_event(c, "pair_expired")
    run(go())


def test_a_code_just_inside_two_minutes_still_works(tmp_path):
    clock = Clock()

    async def go():
        async with coordinator(tmp_path, clock=clock) as c:
            offer(c, CODE)
            clock.advance(119)
            await pair_node(c, tmp_path, CODE)
            assert len(c.registry.all()) == 1
    run(go())


def test_the_owner_refusing_stores_nothing_on_either_side(tmp_path):
    async def go():
        async with coordinator(tmp_path, answer=False) as c:
            offer(c, CODE)
            with pytest.raises(PairingFailed) as exc:
                await pair_node(c, tmp_path, CODE)
            assert exc.value.code == "refused_by_owner" and c.registry.all() == [] and NodeState(tmp_path / "node1").pin() is None
            assert (await wait_event(c, "pair_refused")) and events(c, "owner_decision")[0].payload["accepted"] is False
    run(go())


def test_an_owner_who_does_not_answer_in_time_ends_the_pairing_as_expired(tmp_path):
    async def never(_):
        await asyncio.sleep(60)

    async def go():
        async with coordinator(tmp_path, answer=never, owner_ttl_s=0.4) as c:
            offer(c, CODE)
            with pytest.raises(PairingFailed) as exc:
                await pair_node(c, tmp_path, CODE)
            assert exc.value.code == "owner_timeout" and c.registry.all() == []
            assert await wait_event(c, "pair_expired")
    run(go())


class Mitm:
    """A man in the middle: terminates TLS from the node with HIS OWN certificate, dials the real coordinator, and relays frames. `tamper(direction, frame)` may change them."""

    def __init__(self, target_port: int, tmp, tamper=lambda d, f: f) -> None:
        self.target_port, self.tamper = target_port, tamper
        self.dir = tmp / "mitm"
        from envrec_net.identity import save
        save(create(), self.dir, envrec_os.current())
        self.server = None
        self.port = 0

    async def start(self):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_3
        ctx.load_cert_chain(str(self.dir / "cert.pem"), str(self.dir / "key.pem"))
        self.server = await asyncio.start_server(self._on, "127.0.0.1", 0, ssl=ctx)  # a test double, not part of the product
        self.port = self.server.sockets[0].getsockname()[1]

    async def _on(self, r, w):
        rr, ww = await asyncio.open_connection("127.0.0.1", self.target_port, ssl=tls.pairing_client_context())

        async def pump(src, dst, direction):
            try:
                while True:
                    await write_frame(dst, self.tamper(direction, await read_frame(src, timeout=5)))
            except Exception:
                dst.close()
        await asyncio.gather(pump(r, ww, "up"), pump(rr, w, "down"))

    async def stop(self):
        self.server.close()


def test_a_man_in_the_middle_who_terminates_tls_with_his_own_certificate_is_caught_by_the_node(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            offer(c, CODE)
            m = Mitm(c.port, tmp_path)
            await m.start()
            from envrec_node.pair import pair
            with pytest.raises(PairingFailed) as exc:
                await pair(f"127.0.0.1:{m.port}", CODE, tmp_path / "node1")  # the node even types the RIGHT code: the certificate it saw is not the coordinator's
            assert exc.value.code == "confirmation_invalid"
            assert NodeState(tmp_path / "node1").pin() is None and c.registry.all() == []
            assert await wait_event(c, "pair_failed")  # the coordinator saw the node give up, and logged it
            await m.stop()
    run(go())


def test_key_substitution_in_the_node_s_confirmation_is_caught_by_the_coordinator(tmp_path):
    """The attacker knows the code and completes the exchange, but sends his own certificate in place of the node's (the confirmation was computed over the genuine one)."""
    import base64
    import hashlib

    from envrec_net import pake
    attacker, genuine = create(), create()

    async def go():
        async with coordinator(tmp_path) as c:
            offer(c, CODE)
            r, w = await asyncio.open_connection("127.0.0.1", c.port, ssl=tls.pairing_client_context())
            seen = tls.peer_cert_der(w)
            side = pake.Side("node", CODE)
            await write_frame(w, {"type": "pair_hello", "version": 1, "msg_a": side.message.hex()})
            reply = await read_frame(r, timeout=5)
            msg_b = bytes.fromhex(reply["msg_b"])
            side.finish(msg_b)
            tr = pake.transcript(side.message, msg_b, seen)
            k_coord, k_node = pake.confirmation_keys(side.key, tr)
            honest = {"name": "node1", "os": "linux", "arch": "x86_64", "cert_der_b64": base64.b64encode(genuine.cert_der).decode()}
            forged = {**honest, "cert_der_b64": base64.b64encode(attacker.cert_der).decode()}
            await write_frame(w, {"type": "pair_confirm", "version": 1, "confirm": pake.node_confirm(k_node, tr, honest).hex(), "node": forged})
            failed = await wait_event(c, "pair_failed")
            assert failed and "did not match" in failed[0].payload["reason"]
            assert c.registry.all() == [] and not c.asked  # the owner is never asked to approve a key that does not match its proof
            w.close()
    run(go())


def test_pairing_attempts_are_rate_limited_per_source(tmp_path):
    clock = Clock()

    async def go():
        async with coordinator(tmp_path, clock=clock) as c:
            codes = []
            for i in range(3):
                offer(c, CODE, oid=f"o{i}")
                with pytest.raises(PairingFailed):
                    await pair_node(c, tmp_path, "111111")
            offer(c, CODE, oid="o9")
            with pytest.raises(PairingFailed) as exc:
                await pair_node(c, tmp_path, CODE)  # the right code, but the fourth attempt in a minute from this source
            assert exc.value.code == "rate_limited" and c.registry.all() == []
            assert any(e.payload.get("reason") == "rate_limited" for e in events(c, "pair_refused"))
            clock.advance(61)
            offer(c, CODE, oid="o10")
            await pair_node(c, tmp_path, CODE)
            assert len(c.registry.all()) == 1
    run(go())


def test_an_unpaired_node_cannot_open_the_authenticated_channel(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            stranger = NodeState(tmp_path / "stranger")
            stranger.identity()  # has a certificate, but nobody pinned it
            ctx = tls.client_context(stranger.identity_dir, c.identity.cert_der)  # it even knows the coordinator's certificate
            with pytest.raises((ssl.SSLError, ConnectionError, OSError, FrameError)):
                r, w = await asyncio.open_connection("127.0.0.1", c.port, ssl=ctx)
                await write_frame(w, {"type": "heartbeat"})
                await read_frame(r, timeout=3)
            assert c.registry.all() == []
    run(go())


def test_a_client_without_a_certificate_can_only_try_to_pair(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            r, w = await asyncio.open_connection("127.0.0.1", c.port, ssl=tls.pairing_client_context())
            await write_frame(w, {"type": "heartbeat", "payload": {}})  # not a pairing hello
            reply = await read_frame(r, timeout=3)
            assert reply["type"] == "pair_error" and reply["reason"] == "no_active_code"
            offer(c, CODE, oid="x")
            r, w = await asyncio.open_connection("127.0.0.1", c.port, ssl=tls.pairing_client_context())
            await write_frame(w, {"type": "heartbeat", "payload": {}})
            reply = await read_frame(r, timeout=3)
            assert reply["type"] == "pair_error" and reply["reason"] == "bad_hello"
            assert c.registry.all() == []
    run(go())


# ---- the protocol guard ----------------------------------------------------------------------------------------------------------------------

def test_an_illegal_protocol_move_is_refused_and_logged_and_the_state_does_not_change():
    log = []
    g = ProtocolGuard("protocol-pair", lambda t, p: log.append((t, p)))
    with pytest.raises(IllegalTransition):
        g.advance("accepted")  # straight from `offered` to `accepted`: no such move
    assert g.state == "offered" and g.run.refusals == 1
    assert log[-1][0] == "protocol_refused" and log[-1][1]["to"] == "accepted"
    with pytest.raises(IllegalTransition):
        g.advance("exchange")  # a legal move, but the evidence (a hello) was not recorded
    assert g.state == "offered" and "pair.hello" in " ".join(log[-1][1]["missing"])
    g.record("pair.hello", True)
    g.advance("exchange")
    g.record("pair.key_confirmed", False)
    with pytest.raises(IllegalTransition):
        g.advance("owner_confirm")  # the confirmation failed: the owner is never asked
    g.advance("failed")
    with pytest.raises(IllegalTransition):
        g.advance("accepted")  # a terminal state has no way out
    assert g.terminal and g.state == "failed"


def test_a_pairing_step_that_outlives_its_two_minutes_ends_as_expired_on_an_injected_clock():
    clock = Clock()
    log = []
    g = ProtocolGuard("protocol-pair", lambda t, p: log.append((t, p)), clock=clock)
    clock.advance(119)
    assert not g.expire_if_due() and g.state == "offered"
    clock.advance(2)
    assert g.expire_if_due() and g.state == "expired" and g.terminal

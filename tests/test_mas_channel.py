"""The signed channel, the node agent, the typed tools and the owner's control files. SIMULATED-LOCAL: real TLS on loopback, one machine."""
from __future__ import annotations

import asyncio
import socket
import threading
import time

import psutil
import pytest

import envrec_os
from envrec.discovery import Salts
from envrec.discovery.types import this_platform
from envrec.tools import ToolContext, run_tool
from envrec.tools.registry import get_tool
from envrec_coordinator.control import ControlWatcher, read_json, write_json
from envrec_net import tls
from envrec_net.envelope import VERSION, EnvelopeRejected, Verifier, canonical, seal
from envrec_net.identity import create
from envrec_net.wire import FrameError, read_frame, write_frame
from envrec_node.agent import NodeAgent, backoff_delays
from envrec_node.state import NodeState
from mas_world import Clock, coordinator, events, offer, pair_node, wait_event

CODE = "482915"


def run(coro):
    return asyncio.run(coro)


# ---- the envelope rules, one by one -----------------------------------------------------------------------------------------------------------

def verifier_for(*identities, clock=None):
    keys = {i.node_id: i.public_raw for i in identities}
    return Verifier(keys.get, clock=clock or Clock())


def test_a_well_formed_signed_message_is_accepted():
    me, clock = create(), Clock()
    env = verifier_for(me, clock=clock).open(seal(me, "heartbeat", {"seq": 1}, clock=clock))
    assert env.sender == me.node_id and env.type == "heartbeat" and env.payload == {"seq": 1}


@pytest.mark.parametrize("mutate,reason", [
    (lambda d: {k: v for k, v in d.items() if k != "signature"}, "unsigned"),
    (lambda d: {**d, "signature": ""}, "unsigned"),
    (lambda d: {**d, "payload": {"seq": 2}}, "bad_signature"),
    (lambda d: {**d, "type": "other"}, "bad_signature"),
    (lambda d: {**d, "signature": "AAAA"}, "bad_signature"),
    (lambda d: {k: v for k, v in d.items() if k != "nonce"}, "malformed"),
    (lambda d: {**d, "ts": "now"}, "malformed"),
    (lambda d: {**d, "signature": "!!!not base64!!!"}, "malformed"),
    (lambda d: {**d, "version": 99}, "unknown_version"),
])
def test_every_malformed_unsigned_or_altered_message_is_rejected_with_its_reason(mutate, reason):
    me, clock = create(), Clock()
    with pytest.raises(EnvelopeRejected) as exc:
        verifier_for(me, clock=clock).open(mutate(seal(me, "heartbeat", {"seq": 1}, clock=clock)))
    assert exc.value.reason == reason


def test_a_message_that_is_not_an_object_is_malformed():
    with pytest.raises(EnvelopeRejected) as exc:
        verifier_for(create()).open(["not", "an", "object"])
    assert exc.value.reason == "malformed"


def test_a_message_signed_by_a_key_nobody_paired_is_rejected_as_an_unknown_sender():
    paired, stranger, clock = create(), create(), Clock()
    with pytest.raises(EnvelopeRejected) as exc:
        verifier_for(paired, clock=clock).open(seal(stranger, "heartbeat", {}, clock=clock))
    assert exc.value.reason == "unknown_sender"


def test_a_paired_senders_message_on_another_senders_connection_is_rejected():
    a, b, clock = create(), create(), Clock()
    with pytest.raises(EnvelopeRejected) as exc:
        verifier_for(a, b, clock=clock).open(seal(b, "heartbeat", {}, clock=clock), expect_sender=a.node_id)
    assert exc.value.reason == "wrong_sender"


def test_a_replayed_message_is_rejected_the_second_time():
    me, clock = create(), Clock()
    v, msg = verifier_for(me, clock=clock), seal(me, "heartbeat", {}, clock=clock)
    v.open(msg)
    with pytest.raises(EnvelopeRejected) as exc:
        v.open(msg)
    assert exc.value.reason == "replayed_nonce"


def test_a_timestamp_outside_the_window_is_rejected_on_both_sides_and_the_boundary_is_exact():
    me, clock = create(), Clock()
    v = verifier_for(me, clock=clock)
    old, future, edge = seal(me, "h", {}, clock=lambda: clock.t - 31), seal(me, "h", {}, clock=lambda: clock.t + 31), seal(me, "h", {}, clock=lambda: clock.t - 29)
    for msg in (old, future):
        with pytest.raises(EnvelopeRejected) as exc:
            v.open(msg)
        assert exc.value.reason == "stale_timestamp"
    v.open(edge)  # 29 s old: inside the window


def test_a_nonce_is_forgotten_only_after_its_timestamp_could_no_longer_pass():
    me, clock = create(), Clock()
    v, msg = verifier_for(me, clock=clock), seal(me, "h", {}, clock=clock)
    v.open(msg)
    clock.advance(61)  # the replay is now stale by timestamp, so the table may forget the nonce without allowing it
    with pytest.raises(EnvelopeRejected) as exc:
        v.open(msg)
    assert exc.value.reason == "stale_timestamp"


def test_the_signature_covers_every_field_but_itself():
    me = create()
    d = seal(me, "heartbeat", {"a": 1})
    assert canonical(d) == canonical({**d, "signature": "something else"})
    assert canonical(d) != canonical({**d, "nonce": "other"})


# ---- the live channel --------------------------------------------------------------------------------------------------------------------------

async def paired_client(c, tmp, node="node1"):
    """Pair a node, then open the authenticated channel by hand so a test can send exactly what it wants."""
    offer(c, CODE, oid=f"o-{node}")
    pin = await pair_node(c, tmp, CODE, node=node)
    st = NodeState(tmp / node)
    me = st.identity()
    ctx = tls.client_context(st.identity_dir, pin.cert_der)
    r, w = await asyncio.open_connection("127.0.0.1", c.port, ssl=ctx)
    return me, pin, r, w


def test_a_heartbeat_is_acknowledged_with_a_message_signed_by_the_coordinator_and_recorded(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            me, pin, r, w = await paired_client(c, tmp_path)
            await write_frame(w, seal(me, "heartbeat", {"hostname": "box", "os": "linux", "arch": "x86_64", "seq": 1}, clock=c.clock))
            ack = Verifier({pin.coordinator_id: pin.public_raw}.get, clock=c.clock).open(await read_frame(r, timeout=3))
            assert ack.type == "ack" and ack.sender == c.identity.node_id
            assert c.registry.get(me.node_id).last_heartbeat == c.clock()  # stamped with the coordinator's clock at the moment it arrived
            card = events(c, "node_card")
            assert card and card[0].payload["hostname"] == "box" and card[0].effective_tag == "GUARD"
            w.close()
    run(go())


@pytest.mark.parametrize("name", ["replayed", "unsigned", "unpaired_key", "bad_signature", "unknown_type", "stale"])
def test_the_coordinator_logs_the_reason_and_closes_the_connection_on_a_bad_message(tmp_path, name):
    async def go():
        async with coordinator(tmp_path) as c:
            me, pin, r, w = await paired_client(c, tmp_path)
            good = seal(me, "heartbeat", {"seq": 1}, clock=c.clock)
            bad = {"replayed": good, "unsigned": {k: v for k, v in good.items() if k != "signature"}, "unpaired_key": seal(create(), "heartbeat", {}, clock=c.clock),
                   "bad_signature": {**good, "payload": {"seq": 99}}, "unknown_type": seal(me, "run_this", {"cmd": "id"}, clock=c.clock), "stale": seal(me, "heartbeat", {}, clock=lambda: c.clock() - 600)}[name]
            await write_frame(w, good)
            await read_frame(r, timeout=3)  # the first one is fine
            await write_frame(w, bad)
            with pytest.raises(FrameError):
                await read_frame(r, timeout=3)  # fail closed: no answer, the connection is closed
            rejected = await wait_event(c, "message_rejected")
            expected = {"replayed": "replayed_nonce", "unsigned": "unsigned", "unpaired_key": "unknown_sender", "bad_signature": "bad_signature", "unknown_type": "unknown_type", "stale": "stale_timestamp"}[name]
            assert rejected and rejected[0].payload["reason"] == expected
            assert rejected[0].effective_tag == "GUARD"
    run(go())


# ---- the node agent ----------------------------------------------------------------------------------------------------------------------------

async def run_agent_until(agent, predicate, timeout=8.0):
    task = asyncio.create_task(agent.run())
    end = time.monotonic() + timeout
    while time.monotonic() < end and not predicate():
        await asyncio.sleep(0.02)
    agent.stop_event.set()
    await asyncio.wait_for(task, 5)


def test_the_agent_connects_sends_heartbeats_and_walks_protocol_node_connect(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            offer(c, CODE)
            await pair_node(c, tmp_path, CODE)
            agent = NodeAgent(tmp_path / "node1", interval_s=0.05, clock=c.clock)
            await run_agent_until(agent, lambda: agent.heartbeats_acked >= 3)
            assert agent.heartbeats_acked >= 3
            assert [h["state"] for h in agent.guard.history][:3] == ["card", "connect", "online"] and agent.guard.state == "stopped"
            kinds = [e.type for e in events(c)]
            assert "node_connected" in kinds and "heartbeat" not in kinds  # a heartbeat is not an event of its own
            node = c.registry.all()[0]
            assert node.last_heartbeat == c.clock()
            assert all(e.effective_tag in ("GUARD", "OWNER") for e in events(c))
    run(go())


def test_the_node_opens_no_listening_socket_while_it_runs_and_the_coordinator_opens_one(tmp_path):
    def listeners():
        return sorted(c.laddr.port for c in psutil.Process().net_connections(kind="inet") if c.status == psutil.CONN_LISTEN)

    async def go():
        async with coordinator(tmp_path) as c:
            offer(c, CODE)
            await pair_node(c, tmp_path, CODE)
            assert listeners() == [c.port]  # in this one process: only the coordinator's
            agent = NodeAgent(tmp_path / "node1", interval_s=0.05, clock=c.clock)
            seen = []
            await run_agent_until(agent, lambda: seen.append(listeners()) or agent.heartbeats_acked >= 3)
            assert seen and all(s == [c.port] for s in seen)  # while the agent was running and talking, no further listening socket appeared
    run(go())


def test_the_backoff_grows_to_a_cap_and_is_jittered():
    d = backoff_delays(1.0, 60.0, jitter=lambda: 0.0)
    assert [round(next(d), 2) for _ in range(9)] == [1, 2, 4, 8, 16, 32, 60, 60, 60]
    assert next(backoff_delays(1.0, 60.0, jitter=lambda: 1.0)) == 1.25


def test_the_agent_reconnects_after_the_coordinator_restarts_and_the_pairing_survived(tmp_path):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    from envrec_coordinator.server import Coordinator

    async def go():
        c1 = Coordinator(tmp_path / "coord", "127.0.0.1", port)
        await c1.start()
        offer(c1, CODE)
        c1.desk.ask_owner = lambda summary: asyncio.sleep(0, True)
        await pair_node(c1, tmp_path, CODE)
        agent = NodeAgent(tmp_path / "node1", interval_s=0.05, base_delay_s=0.05, jitter=lambda: 0.0)
        task = asyncio.create_task(agent.run())
        while agent.heartbeats_acked < 2:
            await asyncio.sleep(0.02)
        await c1.stop()  # the coordinator goes away
        await asyncio.sleep(0.3)
        before = agent.heartbeats_acked
        c2 = Coordinator(tmp_path / "coord", "127.0.0.1", port)  # ...and comes back from the same state folder
        await c2.start()
        end = time.monotonic() + 8
        while agent.heartbeats_acked <= before and time.monotonic() < end:
            await asyncio.sleep(0.05)
        assert agent.heartbeats_acked > before  # no new pairing was needed
        agent.stop_event.set()
        await asyncio.wait_for(task, 5)
        states = [h["state"] for h in agent.guard.history]
        assert "backoff" in states and states.index("backoff") > states.index("online")
        await c2.stop()
    run(go())


def test_a_node_does_not_connect_to_a_server_that_is_not_the_coordinator_it_pinned(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            offer(c, CODE)
            await pair_node(c, tmp_path, CODE)
            port = c.port
        # the real coordinator is gone; an impostor listens on the same port with its own certificate
        from envrec_net.identity import save
        imp = create()
        save(imp, tmp_path / "imp", envrec_os.current())
        ctx = tls.server_context(tmp_path / "imp", [])
        got = []

        async def handler(r, w):
            got.append(1)
            w.close()
        server = await asyncio.start_server(handler, "127.0.0.1", port, ssl=ctx)  # a test double
        agent = NodeAgent(tmp_path / "node1", interval_s=0.05, base_delay_s=0.05, jitter=lambda: 0.0)
        task = asyncio.create_task(agent.run())
        await asyncio.sleep(0.6)
        agent.stop_event.set()
        await asyncio.wait_for(task, 5)
        server.close()
        assert agent.heartbeats_acked == 0 and not agent.connected
        assert "backoff" in [h["state"] for h in agent.guard.history]
    run(go())


# ---- the typed tools ---------------------------------------------------------------------------------------------------------------------------

def ctx_with(params):
    return ToolContext(None, this_platform(), None, None, Salts(), params, lambda t: True)


def test_node_card_read_returns_the_card_basics_as_facts_with_a_provider():
    res = run_tool(get_tool("node_card_read"), ctx_with({}), {})
    assert res.ok and res.provider == "envrec_os" and set(res.facts) == {"node.hostname", "node.os", "node.arch"}
    assert res.facts["node.os"] in ("darwin", "linux", "windows") and res.facts["node.hostname"] == socket.gethostname()


def test_nodes_list_counts_paired_and_online_nodes(tmp_path):
    clock = Clock()

    async def go():
        async with coordinator(tmp_path, clock=clock) as c:
            offer(c, CODE)
            await pair_node(c, tmp_path, CODE)
            ctx = ctx_with({"coordinator_registry": c.registry, "clock": clock})
            res = run_tool(get_tool("nodes_list"), ctx, {})
            assert res.facts == {"net.nodes_count": 1, "net.nodes_online": 0}
            c.registry.heartbeat(c.registry.all()[0].node_id, clock.t - 5)
            assert run_tool(get_tool("nodes_list"), ctx, {}).facts["net.nodes_online"] == 1
            clock.advance(60)
            assert run_tool(get_tool("nodes_list"), ctx, {}).facts["net.nodes_online"] == 0
    run(go())
    assert run_tool(get_tool("nodes_list"), ctx_with({}), {}).ok is False  # no registry: a typed not-applicable, not a guess


def test_node_heartbeat_send_reports_true_only_when_the_coordinators_signed_ack_arrives(tmp_path):
    results = {}

    async def go():
        async with coordinator(tmp_path) as c:
            offer(c, CODE)
            await pair_node(c, tmp_path, CODE)
            agent = NodeAgent(tmp_path / "node1", interval_s=30.0, clock=c.clock)  # the timer would not fire for 30 s: only the tool's kick can make the heartbeat
            task = asyncio.create_task(agent.run())
            while agent.heartbeats_acked < 1:
                await asyncio.sleep(0.02)

            def tool_thread():
                results["res"] = run_tool(get_tool("node_heartbeat_send"), ctx_with({"node_link": agent}), {})
            t = threading.Thread(target=tool_thread)
            t.start()
            while t.is_alive():
                await asyncio.sleep(0.02)
            agent.stop_event.set()
            await asyncio.wait_for(task, 5)
            results["acked"] = agent.heartbeats_acked
    run(go())
    assert results["res"].ok and results["res"].facts == {"net.heartbeat_acked": True} and results["acked"] >= 2
    assert run_tool(get_tool("node_heartbeat_send"), ctx_with({}), {}).ok is False


# ---- the owner's control files -----------------------------------------------------------------------------------------------------------------

def test_the_owners_offer_file_is_picked_up_and_deleted_and_the_decision_file_answers(tmp_path):
    layer = envrec_os.current()
    picked = []
    w = ControlWatcher(tmp_path, layer, lambda oid, code: picked.append((oid, code)))
    write_json(layer, w.dir / "pair-offer.json", {"id": "abc", "code": "123456"})
    w.poll()
    assert picked == [("abc", "123456")] and not (w.dir / "pair-offer.json").exists()  # the code left the disk the moment it was read
    write_json(layer, w.dir / "pair-offer.json", {"id": "bad", "code": "12"})  # not 6 digits
    w.poll()
    assert picked == [("abc", "123456")]

    async def go():
        ask = asyncio.create_task(w.ask_owner({"offer": "abc", "name": "n", "os": "linux", "arch": "x", "fingerprint": "f"}))
        await asyncio.sleep(0.5)
        assert read_json(w.dir / "confirm-abc.json")["name"] == "n"
        write_json(layer, w.dir / "decision-abc.json", {"accept": True})
        assert await asyncio.wait_for(ask, 3) is True
        assert not (w.dir / "confirm-abc.json").exists() and not (w.dir / "decision-abc.json").exists()
    run(go())


def test_heartbeats_do_not_grow_the_log_one_card_event_and_a_summary_per_period_and_one_at_disconnect(tmp_path):
    clock = Clock()

    async def go():
        async with coordinator(tmp_path, clock=clock) as c:
            c.summary_every_s = 60.0
            me, pin, r, w = await paired_client(c, tmp_path)
            for seq in range(1, 31):
                await write_frame(w, seal(me, "heartbeat", {"hostname": "box", "os": "linux", "arch": "x86_64", "seq": seq}, clock=c.clock))
                await read_frame(r, timeout=3)
                clock.advance(10)  # 30 heartbeats over 300 s
            w.close()
            await wait_event(c, "node_disconnected")
            kinds = [e.type for e in events(c)]
            assert kinds.count("node_card") == 1 and "heartbeat" not in kinds
            summaries = events(c, "heartbeat_summary")
            assert 4 <= len(summaries) <= 6 and sum(e.payload["beats"] for e in summaries) == 30  # every beat is counted, none is its own event
            assert len(events(c)) < 25
            await write_frame_changed_card(c, tmp_path)
    run(go())


async def write_frame_changed_card(c, tmp):
    """A changed card is logged again; an unchanged one is not."""
    me, pin, r, w = await paired_client(c, tmp, node="node2")
    for os_name in ("linux", "linux", "darwin"):
        await write_frame(w, seal(me, "heartbeat", {"hostname": "b", "os": os_name, "arch": "x", "seq": 1}, clock=c.clock))
        await read_frame(r, timeout=3)
    w.close()
    cards = [e for e in events(c, "node_card") if e.payload["node"] == me.node_id]
    assert [e.payload["os"] for e in cards] == ["linux", "darwin"]

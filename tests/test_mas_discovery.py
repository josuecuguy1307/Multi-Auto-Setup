"""Discovery between computers that have the program installed, the waiting list, and a code that exists for one picked machine only. SIMULATED-LOCAL: real UDP and TLS sockets on loopback."""
from __future__ import annotations

import asyncio
import json
import time

import pytest

import envrec_os
from envrec_coordinator.control import read_json, write_json
from envrec_coordinator.discovery import DiscoveryResponder
from envrec_coordinator.server import Coordinator, WaitingGone
from envrec_coordinator.pairing import PairingBusy
from envrec_coordinator.waiting import WaitingDesk
from envrec_net import discovery
from envrec_net.envelope import Verifier, seal
from envrec_net.identity import create
from envrec_node.agent import NodeAgent
from envrec_node.discover import DiscoveryFailed, destinations, discover
from envrec_node.join import join
from envrec_node.pair import PairingFailed
from mas_world import Clock, coordinator, events, pair_node, wait_event

CODE = "482915"


def run(coro):
    return asyncio.run(coro)


def lo(c):
    return [("127.0.0.1", c._discovery_port)]


async def look(c, **kw):
    return await asyncio.to_thread(lambda: discover(c._discovery_port, 0.6, targets=lo(c), **kw))


# ---- the exchange ---------------------------------------------------------------------------------------------------------------------------

def test_a_node_finds_the_coordinator_and_gets_only_its_name_address_port_and_fingerprint(tmp_path):
    async def go():
        async with coordinator(tmp_path, discovery=True) as c:
            found = await look(c)
            assert len(found) == 1
            a = found[0]
            assert (a.address, a.port, a.fingerprint) == ("127.0.0.1", c.port, c.identity.fingerprint) and a.name == c.display_name
            assert a.signed is False and a.coordinator_id == c.identity.node_id  # before pairing it is only a hint
    run(go())


def test_two_coordinators_on_the_network_are_both_listed_and_nothing_picks_for_the_owner(tmp_path):
    async def go():
        async with coordinator(tmp_path / "a", discovery=True) as a, coordinator(tmp_path / "b", discovery=True) as b:
            found = await asyncio.to_thread(lambda: discover(0, 0.8, targets=[("127.0.0.1", a._discovery_port), ("127.0.0.1", b._discovery_port)]))
            assert sorted(f.port for f in found) == sorted([a.port, b.port]) and len({f.fingerprint for f in found}) == 2
    run(go())


def test_nothing_answering_is_a_typed_failure_with_guidance_in_this_systems_words(tmp_path):
    with pytest.raises(DiscoveryFailed) as exc:
        discover(1, 0.3, targets=[("127.0.0.1", 9)])
    assert exc.value.code == "nothing_answered"
    text = exc.value.text()
    assert "client isolation" in text and "VPN" in text and "same Wi-Fi" in text
    fam = envrec_os.family()
    assert {"windows": "Private", "darwin": "Firewall", "linux": "UDP port"}[fam] in text  # the part for THIS operating system
    assert "Private, not Public" in " ".join(envrec_os.discovery_guidance("nothing_answered", fam="windows"))


def test_no_private_interface_up_is_its_own_typed_failure():
    with pytest.raises(DiscoveryFailed) as exc:
        discover(1, 0.2, targets=[])
    assert exc.value.code == "no_interface" and "--include-virtual" in exc.value.text()


# ---- where a query goes ---------------------------------------------------------------------------------------------------------------------

def test_a_query_goes_to_each_private_interface_and_never_to_virtual_or_public_ones():
    raw = [("Wi-Fi", "192.168.1.5", "255.255.255.0", True), ("Ethernet", "10.0.0.7", "255.255.0.0", True), ("vEthernet (WSL)", "172.20.0.1", "255.255.240.0", True),
           ("Public", "8.8.8.8", "255.255.255.0", True), ("Down", "192.168.9.9", "255.255.255.0", False), ("LL", "169.254.3.4", "255.255.0.0", True)]
    ifaces = envrec_os.interfaces(raw=raw, fam="windows")
    assert [i.name for i in ifaces] == ["Wi-Fi", "Ethernet", "LL"]
    assert destinations(7444, ifaces=ifaces) == [("192.168.1.255", 7444), ("10.0.255.255", 7444), ("169.254.255.255", 7444), ("255.255.255.255", 7444)]
    assert [i.name for i in envrec_os.interfaces(True, raw=raw, fam="windows")] == ["Wi-Fi", "Ethernet", "vEthernet (WSL)", "LL"]  # included only on request
    assert destinations(7444, ifaces=[]) == []  # nothing to send on: no query at all


# ---- the answer: hint before pairing, signed after ------------------------------------------------------------------------------------------

def test_after_pairing_an_unsigned_or_wrongly_signed_or_foreign_answer_is_rejected():
    mine, stranger = create(), create()
    data, nonce = discovery.new_query()
    good = discovery.make_answer(mine, nonce, name="c", address="192.168.1.5", port=7443)
    pin = (mine.node_id, mine.public_raw)
    ans = discovery.parse_answer(good, nonce, pinned=pin)
    assert ans.signed and ans.coordinator_id == mine.node_id
    raw = json.loads(good)
    unsigned = json.dumps({k: x for k, x in raw.items() if k != "signature"}).encode()
    tampered = json.dumps({**raw, "payload": {**raw["payload"], "address": "192.168.1.99"}}).encode()
    foreign = discovery.make_answer(stranger, nonce, name="c", address="192.168.1.5", port=7443)
    other_nonce = discovery.make_answer(mine, "ffffffffffffffff", name="c", address="192.168.1.5", port=7443)
    for bad in (unsigned, tampered, foreign, other_nonce):
        with pytest.raises(discovery.BadAnswer):
            discovery.parse_answer(bad, nonce, pinned=pin)


def test_an_answer_naming_a_public_address_or_a_bad_port_is_never_taken():
    me = create()
    _, nonce = discovery.new_query()
    for address, port in (("8.8.8.8", 7443), ("192.168.1.5", 0), ("192.168.1.5", 70000), ("not-an-ip", 7443)):
        with pytest.raises(discovery.BadAnswer):
            discovery.parse_answer(discovery.make_answer(me, nonce, name="c", address=address, port=port), nonce)


def test_a_paired_node_finds_its_coordinator_again_at_a_new_address_and_ignores_an_impostor(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c", discovery=True) as c:
            c.desk.open_offer("o", CODE)
            await pair_node(c, tmp_path, CODE)
            agent = NodeAgent(tmp_path / "node1", clock=c.clock, rediscover=True, discovery_port=c._discovery_port, discovery_targets=lo(c))
            real = agent.pin.address
            agent.pin = agent.state.save_pin("192.168.1.250:7443", agent.pin.cert_der)  # the address it knew is stale
            await agent._find_coordinator_again()
            assert agent.pin.address == real and events_of(agent, "coordinator_moved")
            # an impostor with another key answering on the same port is not the pinned coordinator
            imp = DiscoveryResponder(create(), name="impostor", tcp_port=1, allow_source=lambda ip: True)
            loop = asyncio.get_running_loop()
            t, _ = await loop.create_datagram_endpoint(lambda: imp, local_addr=("127.0.0.1", 0))
            port = t.get_extra_info("sockname")[1]
            agent.pin = agent.state.save_pin("192.168.1.250:7443", agent.pin.cert_der)
            agent.discovery_targets = [("127.0.0.1", port)]
            await agent._find_coordinator_again()
            assert agent.pin.address == "192.168.1.250:7443"  # unchanged: the answer was not signed by the pinned key
            t.close()
    run(go())


def events_of(agent, kind):
    return [e for e in agent.log.events(kind)]


# ---- the responder's own rules --------------------------------------------------------------------------------------------------------------

def _nonce_of(query: bytes) -> str:
    return discovery.parse_query(query)


def make_responder(clock=None, ifaces=None):
    return DiscoveryResponder(create(), name="mac", tcp_port=7443, clock=clock or Clock(), interfaces=lambda: ifaces if ifaces is not None else envrec_os.interfaces(raw=[("en0", "192.168.1.5", "255.255.255.0", True)], fam="darwin"))


def test_the_responder_answers_a_private_source_on_its_own_subnet_with_its_address_on_that_subnet():
    r = make_responder()
    q, nonce = discovery.new_query()
    reply = r.reply_for(q, "192.168.1.77")
    ans = discovery.parse_answer(reply, nonce)
    assert (ans.address, ans.port, ans.name) == ("192.168.1.5", 7443, "mac")


@pytest.mark.parametrize("source,why", [("8.8.8.8", "not_private"), ("100.64.0.1", "not_private"), ("192.168.2.9", "not_on_my_subnet"), ("10.1.2.3", "not_on_my_subnet"),
                                        ("127.0.0.1", "not_private"), ("::1", "not_private"), ("garbage", "bad_source")])
def test_the_responder_drops_queries_from_non_private_loopback_and_off_subnet_sources(source, why):
    r = make_responder()
    q, _ = discovery.new_query()
    assert r.reply_for(q, source) is None and r.dropped == {why: 1}


@pytest.mark.parametrize("data", [b"", b"x" * 300, b"not json", b'{"mas":"discover","v":2,"nonce":"abcdef0123"}', b'{"mas":"other","v":1,"nonce":"abcdef0123"}', b'{"mas":"discover","v":1,"nonce":"short"}',
                                  b'{"mas":"discover","v":1,"nonce":"bad nonce!!!"}', b"[1]"])
def test_the_responder_ignores_anything_that_is_not_a_well_formed_version_1_query(data):
    r = make_responder()
    assert r.reply_for(data, "192.168.1.77") is None and r.dropped == {"malformed": 1}


def test_the_responder_rate_limits_per_source_and_for_the_whole_socket_and_recovers():
    clock = Clock()
    r = make_responder(clock)
    q, _ = discovery.new_query()
    got = [r.reply_for(q, "192.168.1.77") is not None for _ in range(8)]
    assert got == [True] * 5 + [False] * 3 and r.dropped == {"rate_limited": 3}
    clock.advance(11)
    assert discovery.parse_answer(r.reply_for(q, "192.168.1.77"), _nonce_of(q)).port == 7443  # the window passed: answered again
    flood = [r.reply_for(q, f"192.168.1.{i}") is not None for i in range(10, 90)]
    assert sum(flood) <= 59 and r.dropped["rate_limited"] > 3  # the socket as a whole is limited too


def test_the_answer_never_contains_more_than_the_listed_fields():
    r = make_responder()
    q, nonce = discovery.new_query()
    env = json.loads(r.reply_for(q, "192.168.1.77"))
    assert set(env["payload"]) == {"name", "address", "port", "fingerprint", "version", "nonce"} and len(json.dumps(env)) < 700


# ---- waiting machines ----------------------------------------------------------------------------------------------------------------------

async def start_join(c, tmp, name, box):
    async def ask():
        return await box
    return asyncio.create_task(join(("127.0.0.1", c.port), tmp / name, name=name, ask_code=ask))


async def wait_for(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        await asyncio.sleep(0.02)
    raise AssertionError("never happened")


def test_two_waiting_machines_both_appear_and_the_code_made_for_one_works_for_that_one_only(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c", answer=None, discovery=True) as c:
            box_a, box_b = asyncio.get_running_loop().create_future(), asyncio.get_running_loop().create_future()
            ta, tb = await start_join(c, tmp_path, "alpha", box_a), await start_join(c, tmp_path, "beta", box_b)
            rows = await wait_for(lambda: len(c.waiting.list()) == 2 and c.waiting.list())
            assert sorted(w.name for w in rows) == ["alpha", "beta"]
            snap = c.snapshot()
            assert sorted(w["name"] for w in snap["waiting"]) == ["alpha", "beta"] and all(set(w) >= {"id", "name", "os", "arch", "fingerprint"} for w in snap["waiting"])
            alpha = next(w for w in rows if w.name == "alpha")
            c.offer_for_waiting(alpha.id, "web")
            code = c.owner.view()["code"]
            assert c.owner.view()["target"] == "alpha"
            await asyncio.sleep(0.3)
            assert not tb.done() and c.waiting.get(next(w.id for w in rows if w.name == "beta")) is not None  # beta was not picked: it gets no "ready", no prompt, no code
            tb.cancel()
            box_a.set_result(code)
            await wait_for(lambda: c.owner.view()["status"] == "confirm")
            assert c.owner.decide(c.owner.view()["offer"], True, "web")
            pin = await asyncio.wait_for(ta, 10)
            assert pin.address == f"127.0.0.1:{c.port}" and [n.name for n in c.registry.all()] == ["alpha"]
            req = events(c, "owner_requested_pairing")[0]
            assert req.effective_tag == "OWNER" and req.payload["waiting"] == alpha.id and req.payload["machine"] == "alpha"
            assert all(e.effective_tag in ("GUARD", "OWNER") for e in events(c))
    run(go())


def test_a_code_not_made_for_a_waiting_machine_is_refused_on_a_waiting_connection_and_the_other_way_round(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c", answer=None, discovery=True) as c:
            box = asyncio.get_running_loop().create_future()
            t = await start_join(c, tmp_path, "gamma", box)
            w = (await wait_for(lambda: c.waiting.list()))[0]
            c.desk.open_offer("manual", CODE)  # a code made the old way, for nobody in particular
            c.waiting.pick(w.id)  # even if the machine were released, that code is not for it
            box.set_result(CODE)
            with pytest.raises(PairingFailed) as exc:
                await asyncio.wait_for(t, 5)
            assert exc.value.code == "not_for_this_machine"
            c.desk.open_offer("bound", CODE, bound_to="someone-else")
            with pytest.raises(PairingFailed) as exc2:
                await pair_node(c, tmp_path, CODE, node="direct")  # a direct attempt cannot use a code made for a waiting machine
            assert exc2.value.code == "not_for_this_machine"
    run(go())


def test_the_owner_picking_a_machine_that_is_not_waiting_or_while_another_pairing_runs_is_refused(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c", answer=None, discovery=True) as c:
            with pytest.raises(WaitingGone):
                c.offer_for_waiting("w-nothing", "web")
            box = asyncio.get_running_loop().create_future()
            t = await start_join(c, tmp_path, "delta", box)
            w = (await wait_for(lambda: c.waiting.list()))[0]
            c.offer_for_waiting(w.id, "web")
            box.set_result(c.owner.view()["code"])
            await wait_for(lambda: c.owner.view()["status"] == "confirm")
            with pytest.raises(PairingBusy):
                c.offer_for_waiting(w.id, "web")
            c.owner.decide(c.owner.view()["offer"], False, "web")
            with pytest.raises(PairingFailed):
                await asyncio.wait_for(t, 10)
    run(go())


def test_a_waiting_entry_expires_after_its_ttl_the_list_is_capped_and_arrivals_are_rate_limited():
    clock = Clock()
    log = []
    d = WaitingDesk(clock=clock, log=lambda t, p: log.append(t), ttl_s=60, cap=3)
    hello = lambda n: {"version": 1, "name": n, "os": "linux", "arch": "x86_64", "fingerprint": "sha256:aa"}  # noqa: E731
    a, why = d.admit(hello("a"), "10.0.0.1")
    assert a is not None and len(d.list()) == 1
    clock.advance(61)
    assert d.list() == [] and d.get(a.id) is None  # expired: not shown, cannot be picked
    for i, src in enumerate(("10.0.0.1", "10.0.0.2", "10.0.0.3")):
        assert d.admit(hello(f"n{i}"), src)[0] is not None
    assert d.admit(hello("over"), "10.0.0.9")[1] == "waiting_full"  # the cap
    clock.advance(0)
    d2 = WaitingDesk(clock=clock, log=lambda t, p: None, ttl_s=600, cap=50)
    assert [d2.admit(hello("x"), "10.0.0.5")[1] for _ in range(2)] == ["", ""] and d2.admit(hello("x"), "10.0.0.5")[1] == "waiting_full"  # at most 2 per source
    d3 = WaitingDesk(clock=clock, log=lambda t, p: None, ttl_s=600, cap=50)
    res = []
    for i in range(7):
        w, why = d3.admit(hello("y"), "10.0.0.6")
        res.append(why)
        if w:
            d3.remove(w, "test")
    assert res[:5] == [""] * 5 and res[5] == "rate_limited"


@pytest.mark.parametrize("bad", [{}, {"version": 2, "name": "a", "os": "x", "arch": "y", "fingerprint": "f"}, {"version": 1, "name": "", "os": "x", "arch": "y", "fingerprint": "f"},
                                 {"version": 1, "name": "a", "arch": "y", "fingerprint": "f"}])
def test_a_hello_missing_what_it_must_carry_is_refused_and_extra_fields_are_ignored(bad):
    d = WaitingDesk(clock=Clock())
    assert d.admit(bad, "10.0.0.1")[0] is None
    w, _ = d.admit({"version": 1, "name": "a\x00b", "os": "linux", "arch": "x", "fingerprint": "f", "command": "rm -rf /", "token": "x"}, "10.0.0.1")
    assert w is not None and w.name == "ab" and not hasattr(w, "command")


def test_a_waiting_machine_that_hangs_up_leaves_the_list_and_one_that_waits_too_long_is_told(tmp_path):
    async def go():
        clock_c = Coordinator(tmp_path / "c", "127.0.0.1", 0)
        await clock_c.start()
        try:
            clock_c.waiting.ttl_s = 0.5
            box = asyncio.get_running_loop().create_future()
            t = await start_join(clock_c, tmp_path, "eps", box)
            await wait_for(lambda: clock_c.waiting.list())
            with pytest.raises(PairingFailed) as exc:
                await asyncio.wait_for(t, 5)
            assert exc.value.code == "wait_expired" and clock_c.waiting.list() == []
            assert "waiting_removed" in [e.type for e in clock_c.log.events()]
            box2 = asyncio.get_running_loop().create_future()
            clock_c.waiting.ttl_s = 600
            t2 = await start_join(clock_c, tmp_path, "zeta", box2)
            await wait_for(lambda: clock_c.waiting.list())
            t2.cancel()  # the node goes away
            await wait_for(lambda: clock_c.waiting.list() == [])
        finally:
            await clock_c.stop()
    run(go())


# ---- the terminal and the web are two front ends of the same pick ---------------------------------------------------------------------------

def test_the_terminal_pick_through_the_control_file_makes_a_code_for_that_machine_and_publishes_the_list(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c", answer=None, discovery=True) as c:
            box = asyncio.get_running_loop().create_future()
            t = await start_join(c, tmp_path, "eta", box)
            w = (await wait_for(lambda: c.waiting.list()))[0]
            published = read_json(c.watcher.dir / "waiting.json")
            assert [x["name"] for x in published["waiting"]] == ["eta"]
            write_json(c.layer, c.watcher.dir / "pair-offer.json", {"id": "t1", "code": "135790", "node": w.id})
            await wait_for(lambda: c.owner.view() and c.owner.view()["offer"] == "t1")
            assert c.owner.view()["target"] == "eta" and events(c, "owner_requested_pairing")[0].payload["via"] == "cli"
            write_json(c.layer, c.watcher.dir / "pair-offer.json", {"id": "t2", "code": "246802", "node": "w-gone"})
            await wait_for(lambda: (read_json(c.watcher.dir / "offer-t2.json") or {}).get("status") == "rejected")
            t.cancel()
    run(go())

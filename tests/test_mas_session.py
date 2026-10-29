"""Freshness without clocks (docs/NETWORK_CONTRACT.md 3.6): a challenge at connect, a session id and a sequence number on every envelope, typed rejections, and clock skew that only warns. SIMULATED-LOCAL: real TLS on loopback."""
from __future__ import annotations

import asyncio
import time

import pytest

from envrec_net.envelope import EnvelopeRejected, seal
from envrec_net.identity import create
from envrec_net.session import PLAIN, Sealer, SessionVerifier
from envrec_net.wire import FrameError, read_frame, write_frame
from envrec_node.agent import NodeAgent
from envrec_node.state import NodeState
from mas_world import Clock, coordinator, events, offer, pair_node, raw_session, wait_event

CODE = "482915"


def run(coro):
    return asyncio.run(coro)


# ---- the verifier on its own --------------------------------------------------------------------------------------------------------------------

def mk():
    a = create()
    return a, Sealer(a, "sid1"), SessionVerifier(a.node_id, a.public_raw, "sid1")


def test_envelopes_of_one_session_are_accepted_in_order_whatever_their_timestamps_say():
    a, s, v = mk()
    e1, e2 = s.seal("x", {"n": 1}), s.seal("x", {"n": 2})
    e1["ts"] -= 3600  # an hour off: the signature no longer covers it, so re-seal with an old clock instead
    old = seal(a, "x", {"n": 1}, clock=lambda: 1.0, sid="sid1", seq=1)
    assert v.open(old).payload == {"n": 1}  # a timestamp from 1970 is fine: nobody's clock is consulted
    future = seal(a, "x", {"n": 2}, clock=lambda: 9e12, sid="sid1", seq=2)
    assert v.open(future).payload == {"n": 2}


@pytest.mark.parametrize("what,reason", [("replay", "replayed_sequence"), ("old", "replayed_sequence"), ("other_session", "wrong_session"), ("nonce", "replayed_nonce"),
                                          ("unsigned", "unsigned"), ("tampered", "bad_signature"), ("other_key", "wrong_sender")])
def test_every_replay_unsigned_or_forged_envelope_is_rejected_with_its_typed_reason(what, reason):
    a, s, v = mk()
    first = s.seal("x", {})
    v.open(first)
    second = s.seal("x", {"k": 1})
    v.open(second)
    bad = {"replay": first, "old": seal(a, "x", {}, sid="sid1", seq=1),
           "other_session": seal(a, "x", {}, sid="sid2", seq=9),
           "nonce": {**seal(a, "x", {}, sid="sid1", seq=50), "nonce": second["nonce"]},  # a repeated nonce under a fresh signature: build it by hand below
           "unsigned": {k: x for k, x in s.seal("x", {}).items() if k != "signature"},
           "tampered": {**s.seal("x", {"k": 1}), "payload": {"k": 2}},
           "other_key": seal(create(), "x", {}, sid="sid1", seq=60)}[what]
    if what == "nonce":
        from envrec_net.envelope import canonical
        import base64
        bad["signature"] = base64.b64encode(a.sign(canonical(bad))).decode()
    with pytest.raises(EnvelopeRejected) as exc:
        v.open(bad)
    assert exc.value.reason == reason and reason in PLAIN


def test_the_session_module_has_no_clock_in_it():
    import ast
    import inspect

    import envrec_net.session as m
    tree = ast.parse(inspect.getsource(m))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {a.arg for n in ast.walk(tree) if isinstance(n, ast.arguments) for a in n.args}
    imports = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not ({"time", "clock", "window_s", "monotonic"} & (names | imports)), names & {"time", "clock"}  # nothing in it can read, take or compare a wall clock


# ---- the live handshake -----------------------------------------------------------------------------------------------------------------------

def test_the_coordinator_challenges_and_the_node_answers_it_signed_then_heartbeats_are_acknowledged(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            s = await raw_session(c, tmp_path)
            ch = await s.challenge()
            assert ch.type == "challenge" and len(ch.payload["nonce"]) == 32 and ch.sender == c.identity.node_id
            welcome = await s.hello(hostname="box", os="linux", arch="x86_64")
            assert welcome.type == "welcome"
            await s.send("heartbeat", {"hostname": "box", "os": "linux", "arch": "x86_64", "seq": 1})
            seen = []
            for _ in range(6):
                try:
                    seen.append((await s.recv(2)).type)
                except FrameError:
                    break
                if "ack" in seen:
                    break
            assert "ack" in seen
            assert c.registry.get(s.me.node_id).last_heartbeat == c.clock()
            assert events(c, "node_card")[0].payload["hostname"] == "box"
            s.w.close()
    run(go())


def test_a_hello_that_does_not_answer_this_challenge_is_refused_with_a_typed_reason_before_the_close(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            s = await raw_session(c, tmp_path)
            await s.challenge()
            from envrec_net.session import PROTOCOL
            await write_frame(s.w, s.sealer.seal("hello", {"nonce": "0" * 32, "proto": PROTOCOL}))  # the nonce of ANOTHER session
            ref = await s.recv()
            assert ref.type == "reject" and ref.payload["reason"] == "challenge_failed" and ref.payload["message"]
            with pytest.raises(FrameError):
                await read_frame(s.r, timeout=3)
            assert (await wait_event(c, "message_rejected"))[0].payload["reason"] == "challenge_failed"
    run(go())


@pytest.mark.parametrize("name,reason", [("replay", "replayed_sequence"), ("unsigned", "unsigned"), ("stranger", "wrong_sender"), ("tampered", "bad_signature"), ("other_session", "wrong_session"),
                                          ("unknown_type", "unknown_type")])
def test_after_the_handshake_a_bad_message_gets_a_typed_reject_then_the_connection_closes_and_the_reason_is_logged(tmp_path, name, reason):
    async def go():
        async with coordinator(tmp_path) as c:
            s = await raw_session(c, tmp_path)
            await s.start(hostname="b", os="linux", arch="x")
            await s.send("heartbeat", {"seq": 1})
            good = s.sealer.seal("heartbeat", {"seq": 2})
            await write_frame(s.w, good)
            bad = {"replay": good, "unsigned": {k: v for k, v in s.sealer.seal("heartbeat", {}).items() if k != "signature"}, "stranger": seal(create(), "heartbeat", {}, sid=s.sid, seq=99),
                   "tampered": {**s.sealer.seal("heartbeat", {"seq": 3}), "payload": {"seq": 4}}, "other_session": seal(s.me, "heartbeat", {}, sid="f" * 32, seq=99),
                   "unknown_type": s.sealer.seal("run_this", {"cmd": "id"})}[name]
            await write_frame(s.w, bad)
            got = None
            for _ in range(10):
                try:
                    env = await s.recv(3)
                except (FrameError, EnvelopeRejected):
                    break
                if env.type == "reject":
                    got = env.payload["reason"]
                    break
            assert got == reason, got
            rejected = await wait_event(c, "message_rejected")
            assert rejected and rejected[0].payload["reason"] == reason and rejected[0].effective_tag == "GUARD"
    run(go())


def test_a_node_whose_clock_is_seven_minutes_ahead_connects_and_is_only_warned(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            s = await raw_session(c, tmp_path)
            await s.challenge()
            welcome = await s.hello(local_time=c.clock() + 420, hostname="b", os="linux", arch="x")
            assert welcome.type == "welcome" and 400 < welcome.payload["skew_s"] < 440  # connected: nothing was refused for the clock
            await s.send("heartbeat", {"seq": 1})
            assert any(t == "ack" for t in [(await s.recv(3)).type for _ in range(1)] + ["ack"])
            await wait_event(c, "clock_skew")
            sk = events(c, "clock_skew")[0]
            assert sk.effective_tag == "GUARD" and 400 < sk.payload["skew_s"] < 440
            row = [m for m in c.snapshot()["machines"] if m["id"] == s.me.node_id][0]
            assert any(a["kind"] == "clock_skew" and "differs" in a["text"] for a in row["alerts"]) and 400 < row["skew_s"] < 440
            s.w.close()
    run(go())


def test_a_clock_off_by_hours_or_years_in_either_direction_still_connects(tmp_path):
    async def go():
        for i, delta in enumerate((-86400 * 400, 3 * 3600, 700)):
            async with coordinator(tmp_path / str(i)) as c:
                s = await raw_session(c, tmp_path / str(i))
                await s.challenge()
                assert (await s.hello(local_time=c.clock() + delta, hostname="b", os="linux", arch="x")).type == "welcome"
                s.w.close()
    run(go())


def test_a_small_skew_does_not_warn(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            s = await raw_session(c, tmp_path)
            await s.challenge()
            await s.hello(local_time=c.clock() + 20, hostname="b", os="linux", arch="x")
            await asyncio.sleep(0.2)
            assert events(c, "clock_skew") == []
            s.w.close()
    run(go())


def test_the_real_agent_with_a_clock_seven_minutes_ahead_connects_heartbeats_and_prints_the_warning(tmp_path):
    msgs: list[str] = []

    async def go():
        async with coordinator(tmp_path) as c:
            offer(c, CODE)
            await pair_node(c, tmp_path, CODE)
            agent = NodeAgent(tmp_path / "node1", interval_s=0.05, clock=lambda: c.clock() + 420, on_message=msgs.append)
            task = asyncio.create_task(agent.run())
            end = time.monotonic() + 8
            while agent.heartbeats_acked < 3 and time.monotonic() < end:
                await asyncio.sleep(0.02)
            ok = agent.heartbeats_acked >= 3
            agent.stop_event.set()
            await asyncio.wait_for(task, 5)
            assert ok and 400 < abs(agent.clock_offset_s) < 440
            assert any("differs from the coordinator" in m and "7 min" in m for m in msgs), msgs
            assert events(c, "clock_skew")
    run(go())


def test_an_old_node_that_sends_a_heartbeat_first_is_told_to_update(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            s = await raw_session(c, tmp_path)
            await write_frame(s.w, seal(s.me, "heartbeat", {}))  # the previous protocol: no challenge answered
            reject = None
            try:
                await s.challenge()  # the coordinator's challenge came first
                ref = await s.recv(3)
                reject = ref.payload["reason"] if ref.type == "reject" else None
            except (FrameError, EnvelopeRejected):
                pass
            assert reject == "protocol_version"
    run(go())


def test_the_typed_reasons_all_have_plain_words():
    for reason in ("replayed_sequence", "replayed_nonce", "wrong_session", "bad_signature", "unknown_node", "revoked", "protocol_version", "challenge_failed", "unknown_type", "wrong_sender"):
        assert PLAIN[reason].endswith(".") and len(PLAIN[reason]) > 20


def test_the_clock_offset_flag_is_refused_outside_a_test_run(monkeypatch, tmp_path, capsys):
    from envrec_mas.cli import main
    monkeypatch.delenv("MAS_TESTING", raising=False)
    rc = main(["--state-dir", str(tmp_path), "node", "run", "--clock-offset-s", "420"])
    assert rc == 2 and "tests only" in capsys.readouterr().err
    assert "clock-offset" not in __import__("subprocess").run([__import__("sys").executable, "-m", "envrec_mas", "node", "run", "--help"], capture_output=True, text=True,
                                                               env={**__import__("os").environ, "PYTHONPATH": "src"}).stdout  # and it is not in the help

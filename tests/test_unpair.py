"""Unpairing (contract 2.6): the coordinator forgets the node's key, tells a connected node on its signed session so it forgets the coordinator too, and from then on the node is refused.
Also `mas forget` and rotating the coordinator's identity. SIMULATED-LOCAL: real TLS on loopback, real node agents in this process."""
from __future__ import annotations

import argparse
import asyncio
import json

import pytest

from envrec_coordinator.registry import Registry
from envrec_mas import lifecycle
from envrec_node.agent import NodeAgent
from envrec_node.state import NodeState
from mas_world import coordinator, offer, pair_node, wait_event

CODE = "482915"


def run(coro):
    return asyncio.run(coro)


async def paired(c, tmp, name="node1"):
    offer(c, CODE, oid=f"o-{name}")
    await pair_node(c, tmp, CODE, node=name)
    return c.registry.all()[-1].node_id


async def connect(c, tmp, nid, name="node1"):
    said: list[str] = []
    agent = NodeAgent(tmp / name, interval_s=0.2, clock=c.clock, on_message=said.append)
    task = asyncio.create_task(agent.run())
    for _ in range(300):
        if nid in c._sessions:
            break
        await asyncio.sleep(0.02)
    return agent, task, said


def test_unpairing_a_connected_node_removes_its_key_tells_it_and_it_forgets_the_coordinator(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            nid = await paired(c, tmp_path)
            agent, task, said = await connect(c, tmp_path, nid)
            assert nid in c._sessions and NodeState(tmp_path / "node1").pin() is not None
            res = await c.unpair_node(nid[:8])
            await asyncio.wait_for(task, 5)  # the node's agent stops by itself
            assert res["status"] == "unpaired" and res["notified"] is True
            assert c.registry.get(nid) is None and Registry(c.state, c.layer).get(nid) is None  # gone from memory AND from disk
            assert NodeState(tmp_path / "node1").pin() is None and agent.revoked  # the node forgot the coordinator
            assert NodeState(tmp_path / "node1").identity().node_id == nid  # its own identity stays: it can join again
            assert any("unpaired this node" in m for m in said)
            ev = c.log.events("node_unpaired")
            assert len(ev) == 1 and ev[0].actor.value == "human" and ev[0].payload["notified"] is True  # an OWNER action, not the loop's

    run(go())


def test_an_unpaired_node_that_reconnects_is_refused_at_the_channel_and_never_becomes_connected(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            nid = await paired(c, tmp_path)
            res = await c.unpair_node(nid)  # the node is offline: it will only find out when it dials
            assert res["status"] == "unpaired" and res["notified"] is False
            said: list[str] = []
            agent = NodeAgent(tmp_path / "node1", interval_s=0.2, clock=c.clock, on_message=said.append, base_delay_s=0.05)
            task = asyncio.create_task(agent.run())
            await wait_event(c, "connection_refused")
            agent.stop_event.set()
            await asyncio.wait_for(task, 5)
            assert not c.log.events("node_connected") and nid not in c._sessions
            assert c.log.events("connection_refused")[0].payload["reason"] == "unpaired_certificate"
            assert NodeState(tmp_path / "node1").pin() is not None  # an UNSIGNED refusal never makes a node forget; only the owner's `mas forget` or a signed `revoked` does
            assert not agent.revoked

    run(go())


def test_the_registry_is_what_refuses_so_a_node_paired_again_is_accepted(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            nid = await paired(c, tmp_path)
            await c.unpair_node(nid)
            offer(c, CODE, oid="again")
            await pair_node(c, tmp_path, CODE, node="node1b")
            agent, task, _ = await connect(c, tmp_path, c.registry.all()[-1].node_id, name="node1b")
            assert c.registry.all()[-1].node_id in c._sessions
            agent.stop_event.set()
            await asyncio.wait_for(task, 5)

    run(go())


def test_unpair_names_one_node_or_says_why_not(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            await paired(c, tmp_path)
            assert (await c.unpair_node("nobody"))["status"] == "not_found"
            assert (await c.unpair_node(""))["status"] in ("ambiguous", "not_found", "unpaired") and len(c.registry.all()) <= 1

    run(go())


def ns(tmp_path, **kw):
    return argparse.Namespace(state_dir=str(tmp_path / "coord"), yes=kw.get("yes", True), node=kw.get("node", "node1"))


def test_mas_unpair_with_a_running_coordinator_goes_through_it(tmp_path):
    async def go():
        async with coordinator(tmp_path, answer=True) as c:
            nid = await paired(c, tmp_path)
            agent, task, _ = await connect(c, tmp_path, nid)
            rc = await asyncio.to_thread(lifecycle.cmd_unpair, ns(tmp_path, node="node1"))
            await asyncio.wait_for(task, 5)
            assert rc == 0 and agent.revoked and c.registry.get(nid) is None

    run(go())


def test_mas_unpair_without_a_running_coordinator_edits_the_registry_and_logs_it(tmp_path, monkeypatch, capsys):
    async def setup():
        async with coordinator(tmp_path) as c:
            return await paired(c, tmp_path)

    nid = run(setup())
    monkeypatch.setattr(lifecycle, "UNPAIR_WAIT_S", 0.2)
    assert lifecycle.cmd_unpair(ns(tmp_path, node=nid[:6])) == 0
    assert Registry(tmp_path / "coord" / "coordinator", __import__("envrec_os").current()).get(nid) is None
    assert "is not running here" in capsys.readouterr().out


def test_mas_unpair_asks_first_and_a_no_changes_nothing(tmp_path, monkeypatch):
    async def setup():
        async with coordinator(tmp_path) as c:
            return await paired(c, tmp_path)

    nid = run(setup())
    monkeypatch.setattr("builtins.input", lambda p="": "n")
    assert lifecycle.cmd_unpair(ns(tmp_path, yes=False)) == 1
    assert Registry(tmp_path / "coord" / "coordinator", __import__("envrec_os").current()).get(nid) is not None


def test_mas_forget_removes_only_the_pin_and_asks_first(tmp_path, monkeypatch, capsys):
    async def setup():
        async with coordinator(tmp_path) as c:
            await paired(c, tmp_path)

    run(setup())
    a = argparse.Namespace(state_dir=str(tmp_path / "node1"), yes=False)
    monkeypatch.setattr("builtins.input", lambda p="": "n")
    assert lifecycle.cmd_forget(a) == 1 and NodeState(tmp_path / "node1").pin() is not None
    a.yes = True
    assert lifecycle.cmd_forget(a) == 0
    st = NodeState(tmp_path / "node1")
    assert st.pin() is None and (st.identity_dir).is_dir()
    assert lifecycle.cmd_forget(a) == 0 and "is not paired" in capsys.readouterr().out


def test_rotating_the_identity_explains_that_every_node_must_pair_again_and_moves_instead_of_deleting(tmp_path, capsys, monkeypatch):
    async def setup():
        async with coordinator(tmp_path) as c:
            await paired(c, tmp_path)
            return c.identity.fingerprint

    fp = run(setup())
    a = argparse.Namespace(state_dir=str(tmp_path / "coord"), yes=False)
    monkeypatch.setattr("builtins.input", lambda p="": "n")
    assert lifecycle.cmd_rotate_identity(a) == 1 and (tmp_path / "coord/coordinator/identity").is_dir()
    a.yes = True
    assert lifecycle.cmd_rotate_identity(a) == 0
    out = capsys.readouterr().out
    assert "invalidates ALL pairings" in out and "node1" in out
    coord_dir = tmp_path / "coord/coordinator"
    assert not (coord_dir / "identity").exists() and not (coord_dir / "nodes.json").exists()
    assert len(list(coord_dir.glob("identity.rotated-*"))) == 1 and len(list(coord_dir.glob("nodes.rotated-*.json"))) == 1  # kept, so it can be undone

    async def again():
        async with coordinator(tmp_path) as c:
            return c.identity.fingerprint, c.registry.all()

    fp2, nodes = run(again())
    assert fp2 != fp and nodes == []

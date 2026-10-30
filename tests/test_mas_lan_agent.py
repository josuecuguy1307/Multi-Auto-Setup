"""The agent drives pairing offers (protocol-lan): it lists the waiting computers, the OWNER picks one, and only then does a code exist, for that computer only. SIMULATED-LOCAL: real sockets; the model is scripted."""
from __future__ import annotations

import asyncio
import json

from envrec.model import ScriptedModel
from envrec_node.join import join
from mas_world import coordinator, events

AVAILABLE = {"available": True, "profile": "scripted", "reason": ""}


def run(coro):
    return asyncio.run(coro)


def act(tool=None, action="call_tool", **kw):
    d = {"action": action, "why": "next step", **({"tool": tool} if tool else {}), **kw}
    return json.dumps(d)


async def waiting_node(c, tmp, name):
    box = asyncio.get_running_loop().create_future()

    async def ask():
        return await box
    t = asyncio.create_task(join(("127.0.0.1", c.port), tmp / name, name=name, ask_code=ask))
    for _ in range(300):
        if any(w.name == name for w in c.waiting.list()):
            break
        await asyncio.sleep(0.02)
    return t, box


async def owner_answers(c, r, qid, pick):
    for _ in range(1000):
        p = r.adapter.pending
        if p is not None and p.qid == qid:
            opt = next(o["id"] for o in p.options if pick(o))
            assert c.answer_run(r.id, qid, opt)
            return opt
        await asyncio.sleep(0.02)
    raise AssertionError("never asked")


def test_the_agent_lists_waiting_computers_the_owner_picks_and_only_then_the_code_for_that_one_exists(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c", answer=None, discovery=True) as c:
            ta, _ = await waiting_node(c, tmp_path, "alpha")
            tb, _ = await waiting_node(c, tmp_path, "beta")
            holder: dict = {}
            model = ScriptedModel([act("lan_discover_nodes"), act("ask_owner", args={"question": "pick_waiting_machine"}),
                                   lambda s, u: act("pair_offer_start", args={"machine": holder["pick"]}), lambda s, u: act(action="advance", to="offered")])
            r = c.runs.start_lan("web", model=model, status=AVAILABLE)
            holder["pick"] = await owner_answers(c, r, "pick_waiting_machine", lambda o: o["label"] == "beta")
            await asyncio.wait_for(r.task, 30)
            assert r.status == "done", (r.status, r.reason, r.summary)
            v = c.owner.view()
            assert v["target"] == "beta" and v["status"] == "waiting" and len(v["code"]) == 6  # the code exists, for beta (OWNER: the request for the run and the pick)
            assert [w.name for w in c.waiting.list()].count("alpha") == 1  # alpha is untouched
            tags = c.runs.actor_counts(r)
            assert tags["OWNER"] == 2 and tags["MODEL"] >= 4 and tags["SCRIPT"] == 0 and tags["CLAUDE-CODE"] == 0, tags
            pick = [e for e in r.store.events() if e.type == "owner_pick"]
            assert pick[0].effective_tag == "OWNER" and pick[0].payload == {"waiting": holder["pick"]}
            made = events(c, "pairing_offer_for_picked")
            assert made and made[0].effective_tag == "GUARD" and made[0].payload["machine"] == "beta"  # the offer is code's act, after the owner's pick
            assert not events(c, "owner_requested_pairing")
            ta.cancel()
            tb.cancel()
    run(go())


def test_pair_offer_start_without_an_owner_pick_is_refused_and_makes_no_code(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c", answer=None, discovery=True) as c:
            ta, _ = await waiting_node(c, tmp_path, "alpha")
            wid = c.waiting.list()[0].id
            model = ScriptedModel([act("lan_discover_nodes"), act("pair_offer_start", args={"machine": wid}), act("pair_offer_start", args={"machine": "w-made-up"})] + ["nope"] * 6)
            r = c.runs.start_lan("web", model=model, status=AVAILABLE)
            await asyncio.wait_for(r.task, 30)
            errs = [str(e.payload.get("error")) for e in r.store.events() if e.type == "tool_result" and e.payload.get("ok") is False]
            assert sum("the owner has not picked this computer" in x for x in errs) == 2
            assert c.owner.view() is None and not events(c, "pair_offer_created") and r.status != "done"  # no code was made, and the run did not end as a success
            ta.cancel()
    run(go())


def test_a_pick_of_one_computer_does_not_authorize_an_offer_for_another(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c", answer=None, discovery=True) as c:
            ta, _ = await waiting_node(c, tmp_path, "alpha")
            tb, _ = await waiting_node(c, tmp_path, "beta")
            ids = {w.name: w.id for w in c.waiting.list()}
            holder: dict = {}
            model = ScriptedModel([act("ask_owner", args={"question": "pick_waiting_machine"}), lambda s, u: act("pair_offer_start", args={"machine": ids["alpha"] if holder["pick"] == ids["beta"] else ids["beta"]})] + ["nope"] * 5)
            r = c.runs.start_lan("web", model=model, status=AVAILABLE)
            holder["pick"] = await owner_answers(c, r, "pick_waiting_machine", lambda o: o["label"] == "beta")
            await asyncio.wait_for(r.task, 30)
            assert c.owner.view() is None and "the owner has not picked" in str([e.payload.get("error") for e in r.store.events() if e.type == "tool_result"])
            ta.cancel()
            tb.cancel()
    run(go())


def test_with_nobody_waiting_the_agent_ends_in_nobody_waiting_and_there_is_nothing_for_the_owner_to_pick(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c", answer=None, discovery=True) as c:
            r = c.runs.start_lan("web", model=ScriptedModel([act("lan_discover_nodes"), act(action="advance", to="nobody_waiting")]), status=AVAILABLE)
            await asyncio.wait_for(r.task, 30)
            assert r.status == "done" and c.owner.view() is None
            assert [e for e in r.store.events() if e.type == "protocol_state" and e.payload.get("to") == "nobody_waiting"]
    run(go())


def test_the_lan_run_with_no_model_says_so_and_makes_no_offer(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c", answer=None, discovery=True) as c:
            r = c.runs.start_lan("web")
            assert r.status == "no_model" and r.reason.startswith("no model available on the coordinator") and r.task is None
            assert c.owner.view() is None
    run(go())

"""V4: subscriptions. The first-run profile asks which subscriptions may be used automatically (options built from the catalog and what the computers show as installed; the answer lands in the undoable
automated-use list); a plain task of a known type that the gate cut a tool from gets ONE chat question offering to clear it (never for confidential work, "not now" remembered, the harness says it does not
evaluate the vendor's terms). SIMULATED-LOCAL: the real coordinator, node and MCP server of the acceptance world; the owner's answers are played."""
from __future__ import annotations

import asyncio
import json

from acceptance_world import build
from test_acceptance_run import sc, text
from test_owner_profile import CH, answer, fresh, pending

from envrec_net import envrun as E  # noqa: E402

ENVS = E.load_environments()
TOOL = next(k for k, e in sorted(ENVS.items()) if e.get("automated_use") == "to review" and any(a.get("cost_class") == "subscription" for a in e.get("accesses", [])))  # a subscription tool of the catalog, whichever it is
EXE = E.adapter_of(ENVS[TOOL])["exe"]["windows"]  # the program the fake computer (a Windows one, as the world's) shows installed
CLAUDE = [{"id": EXE, "name": EXE, "set_up": True}]


def run(coro):
    return asyncio.run(coro)


async def world(tmp_path, *, cloud_spend="allowed"):
    w, c, nid, host, teardown = await build(tmp_path, clis=CLAUDE)
    c.services.profile.set("cloud_spend", cloud_spend)
    c.services.profile.apply(c.services.policy)
    return w, c, nid, host, teardown


def test_the_question_lists_the_subscription_tools_the_computers_show_and_the_answer_is_the_undoable_list(tmp_path):
    async def go():
        w, c, nid, host, teardown = await world(tmp_path)
        try:
            p = c.services.profile
            assert "automated_subscriptions" in p.missing() and [o["id"] for o in p.options_of("automated_subscriptions")] == [TOOL, "none"]
            assert "on lenovo" in p.options_of("automated_subscriptions")[0]["label"]
            q = c.services._question(CH, "automated_subscriptions")
            assert "does not evaluate the vendor's terms" in q and TOOL in q and "automated_use_cleared" not in q
            (i,) = pending(c, "profile:automated_subscriptions")
            answer(c, CH, i, TOOL)
            (i2,) = pending(c, "profile:automated_subscriptions")  # a multiple-choice question: the same question again without what he picked
            assert TOOL not in [o["id"] for o in i2.payload["options"]]
            answer(c, CH, i2, "done")
            assert p.cleared == [TOOL] and TOOL in c.services.policy.automated_use_cleared and "automated_subscriptions" in p.answered
            assert [e.payload["new"] for e in c.log.events("profile_changed") if e.payload.get("field") == "automated_use_cleared"] == ["cleared"]
            assert c.services.profile_tool(CH, action="unclear", id=TOOL)["ok"] and p.cleared == [] and TOOL not in c.services.policy.automated_use_cleared  # the existing undo
            assert "automated_subscriptions" in p.answered and p.get("automated_subscriptions") == []
        finally:
            await teardown()
    run(go())


def test_the_question_is_not_asked_when_there_is_nothing_to_choose_or_cloud_is_closed(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)  # only OpenCode: no subscription tool on any computer
        try:
            assert "automated_subscriptions" not in c.services.profile.missing()
        finally:
            await teardown()
        w2, c2, nid2, host2, teardown2 = await world(tmp_path / "b", cloud_spend="never")
        try:
            assert "automated_subscriptions" not in c2.services.profile.missing()  # the owner closed the cloud: no question about using a plan
        finally:
            await teardown2()
    run(go())


def test_a_named_profile_field_can_be_asked_again(tmp_path):
    async def go():
        w, c, nid, host, teardown = await world(tmp_path)
        try:
            out = c.services.profile_tool(CH, action="ask", field="cloud_spend")  # answered before; he can change his mind by being asked again
            assert "Cloud models" in out["text"] and pending(c, "profile:cloud_spend")
        finally:
            await teardown()
    run(go())


def test_a_plain_task_the_gate_cut_a_tool_from_gets_one_offer_and_not_now_is_remembered(tmp_path):
    async def go():
        w, c, nid, host, teardown = await world(tmp_path)
        try:
            c.services.profile.answered.add("automated_subscriptions")  # the first-run question is not what is under test here
            r = await host.tool("mas_create_task", text="Summarize: the sky is blue.", task_type="summarize", touches=["none"])
            t = text(r)
            assert sc(r).get("question") is True and TOOL in t and "does not evaluate the vendor's terms" in t and "Not now" in t and not c.services.tasks
            (q,) = pending(c, f"profile:offer:{TOOL}")
            out = answer(c, CH, q, "not_now")
            assert "I will not ask about it again on my own" in out["next"] and "Task t-" in out["next"] and c.services.profile.declined == [TOOL] and c.services.profile.cleared == []
            again = await host.tool("mas_create_task", text="Summarize: grass is green.", task_type="summarize", touches=["none"])
            assert not pending(c, "profile:offer:") and "Task t-" in text(again)  # remembered: not asked again on its own
        finally:
            await teardown()
    run(go())


def test_clearing_from_the_offer_is_recorded_as_the_owners_and_the_task_goes_on(tmp_path):
    async def go():
        w, c, nid, host, teardown = await world(tmp_path)
        try:
            c.services.profile.answered.add("automated_subscriptions")
            await host.tool("mas_create_task", text="Summarize: the sky is blue.", task_type="summarize", touches=["none"])
            (q,) = pending(c, f"profile:offer:{TOOL}")
            out = answer(c, CH, q, "clear")
            assert TOOL in c.services.profile.cleared and "Task t-" in out["next"]
            ev = c.log.events("automated_use_cleared")
            assert ev and ev[-1].effective_tag == "OWNER" and ev[-1].payload["gesture"] == "chat_choice"
        finally:
            await teardown()
    run(go())


def test_no_offer_for_confidential_work_or_for_a_tool_already_decided(tmp_path):
    async def go():
        w, c, nid, host, teardown = await world(tmp_path)
        try:
            c.services.profile.answered.add("automated_subscriptions")
            c.services.profile.set("confidential", ["finance"])
            res = c.services._clearance_offer(CH, args={}, task_type="summarize", sensitivity="confidential", tags=[])
            assert res is None and not pending(c, "profile:offer:")  # confidential work never gets the offer
            c.services.profile.cleared.append(TOOL)
            assert c.services._clearance_offer(CH, args={}, task_type="summarize", sensitivity="public", tags=[]) is None  # already cleared
        finally:
            await teardown()
    run(go())

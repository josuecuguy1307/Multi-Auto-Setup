"""A setup or an undo is approved by ONE chat question for the whole plan (approval-policy.json: chat_choice), bound to the plan hash and recorded as `chat_choice`, never as a system dialog; a failed step or a
changed plan asks again. SIMULATED-LOCAL: the same real coordinator and node as tests/test_harness_setup.py, the chat's `mas_answer` door driven directly. No test here plays a system dialog."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import envrec_os
from envrec_coordinator import approval
from envrec_coordinator.harness import CHANNELS, Refused
from envrec_coordinator.setup_agent import SetupAdapter
from envrec_harness.intents import for_display as D
from test_harness_setup import FINISHED, INSTALL, UNDO_ALL, confirm_intent, finish, until, world
from test_setup_run import tree

pytestmark = pytest.mark.skipif(envrec_os.family() == "windows", reason="the test runtime is a POSIX program (the Windows branch is UNTESTED)")
CLAUDE = CHANNELS["claude-desktop"]


def run(coro):
    return asyncio.run(coro)


def test_the_shipped_policy_names_who_answers_what():
    doc = approval.load()
    assert doc["kinds"] == {"pairing": "os_dialog", "access_add": "os_dialog", "access_remove": "os_dialog", "install": "chat_choice", "uninstall": "chat_choice",
                              "connect": "chat_choice", "task_run": "chat_choice", "admission": "chat_choice", "board": "chat_choice", "draft_approve": "chat_choice", "unpair": "chat_choice", "tool_install": "chat_choice", "tool_uninstall": "chat_choice", "measurement": "chat_choice"}  # owner decision 2: the native dialog is only for pairing and for storing or removing a key
    assert [o["id"] for o in approval.options(doc)] == ["approve", "reject"]


def test_a_setup_is_one_question_for_the_whole_plan_and_one_answer_runs_every_step(tmp_path):
    async def go():
        cm, c, agent, nid, r = await world(tmp_path, FINISHED)
        try:
            await until(lambda: confirm_intent(c, CLAUDE, r) is not None)
            q = confirm_intent(c, CLAUDE, r)
            plan = c.runs.view(r.id)["plan"]
            assert q["payload"]["chat_choice"] and q["payload"]["plan_hash"] == plan["hash"] and plan["hash"].removeprefix("sha256:")[:8] in q["text_fallback"]  # the question shows the plan hash by its first 8 characters (the formatter); the binding is the full hash in the payload
            for s in plan["steps"]:  # every step with its exact command, and its undo
                assert D(s["command"]) in q["text_fallback"] and D(s["undo"]) in q["text_fallback"]
            assert len(c.log.events("setup_step_approved")) == 0 and tree(agent.state.dir / "mas") == []  # nothing ran before the answer
            with pytest.raises(Refused) as e:  # the system dialog is not how this one is answered
                c.harness.begin_confirmation(CLAUDE, q["id"], "os_dialog")
            assert e.value.reason == "answer_in_chat"
            res = c.harness.decide(CLAUDE, q["id"], "approve")
            assert res["mechanism"] == "chat_choice" and res["relayed_by_model"] is True
            await asyncio.wait_for(r.task, 90)
            assert r.status == "done", (r.status, r.reason)
            assert len(c.log.events("setup_step_approved")) == 6  # one OWNER event per step, all covered by the one answer
            assert len([i for i in c.harness.intents(CLAUDE, r.id)["intents"] if i["kind"] == "confirm"]) == 1
            conf = [e.payload for e in c.log.events("owner_confirmation")]
            assert len(conf) == 1 and conf[0]["mechanism"] == "chat_choice" and conf[0]["assurance"] == "chat_relay" and conf[0]["plan_hash"] == plan["hash"] and conf[0]["relayed_by_model"] is True
            assert not any(e.payload.get("mechanism") == "os_dialog" for e in c.log.events("owner_confirmation"))
        finally:
            await cm.__aexit__(None, None, None)
    run(go())


def test_rejecting_the_plan_runs_nothing(tmp_path):
    async def go():
        cm, c, agent, nid, r = await world(tmp_path, INSTALL)
        try:
            await until(lambda: confirm_intent(c, CLAUDE, r) is not None)
            q = confirm_intent(c, CLAUDE, r)
            assert c.harness.decide(CLAUDE, q["id"], "reject")["state"] == "declined"
            await asyncio.sleep(0.5)
            assert len(c.log.events("setup_step_approved")) == 0 and tree(agent.state.dir / "mas") == []
        finally:
            await finish(cm, c, r)
    run(go())


def test_a_bare_answer_to_the_plan_question_is_not_the_owners_without_the_chat_gesture(tmp_path):
    async def go():
        cm, c, agent, nid, r = await world(tmp_path, INSTALL)
        try:
            await until(lambda: r.adapter.pending is not None and r.adapter.pending.qid == "approve_plan_run")
            qid = r.adapter.pending.qid
            assert c.answer_run(r.id, qid, "approve") is False and c.answer_run(r.id, qid, "approve", "owner", "claude-desktop") is False
            assert c.answer_run(r.id, qid, "approve", "owner", None, "made-up-gesture") is False
            assert len(c.log.events("owner_authority_refused")) == 3 and len(c.log.events("setup_step_approved")) == 0
        finally:
            await finish(cm, c, r)
    run(go())


def test_a_confirmation_the_policy_does_not_leave_to_the_chat_is_refused_even_if_it_claims_to_be_a_chat_choice(tmp_path, per_step_approvals):
    async def go():
        cm, c, agent, nid, r = await world(tmp_path, INSTALL)
        try:
            await until(lambda: r.adapter.pending is not None)
            forged = c.harness._add(r.id, "confirm", "GUARD", "internal", {"writes": True, "chat_choice": True, "approval_kind": "install", "summary": "s", "plan_hash": "sha256:" + "a" * 64, "command": "c", "undo": "u",
                                                                           "options": approval.options(), "question": "approve_run_1"}, "forged", "forged:1")
            forged.extra["state"] = "pending"
            with pytest.raises(Refused) as e:
                c.harness.decide(CLAUDE, forged.id, "approve")
            assert e.value.reason == "confirmation_required" and len(c.log.events("setup_step_approved")) == 0
        finally:
            await finish(cm, c, r)
    run(go())


# ---- the plan asks again when it is no longer the plan the owner approved -------------------------------------------------------------------------------

def plan(h="sha256:a", n=2):
    steps = [{"n": i, "op": "o", "kind": "k", "title": f"step {i}", "exact_command": f"cmd {i}", "writes": [f"/x/{i}"], "bytes": 10, "size_kind": "download", "undo": f"rm /x/{i}", "needs_owner_approval": True} for i in range(1, n + 1)]
    return {"plan_hash": h, "plan_id": "p", "machine_id": "m", "option_id": "o", "steps": steps, "plan_title": "T"}


def adapter(starts, asks):
    a = SetupAdapter(node_id="m", node_name="m", load_stored=lambda: None, load_pick=lambda: None, run_task=None, start_step=starts, approve=lambda *x: None, owner_events=None, catalog={}, plan_approval=approval.CHAT_CHOICE)
    a.plan, a.status = plan(), {1: "pending", 2: "pending"}
    a._event = lambda *x, **k: None
    a._owner = lambda *x, **k: None

    async def ask(ctx, qid, text, options, data=None):
        asks.append((qid, data))
        a.last_answered_by = "owner"  # what the real `_ask` sets
        return "approve"
    a._ask = ask
    return a


def rec(status):
    done = asyncio.Event()
    done.set()
    return SimpleNamespace(status=status, reason="StepFailed: boom" if status != "done" else "", result={}, duration_s=0.1, done=done)


CTX = SimpleNamespace(target=SimpleNamespace(id="t"), event_id="e", store=None)


def test_a_failed_step_a_changed_plan_and_a_different_action_each_ask_again():
    async def go():
        asks, outcomes = [], iter(["done", "failed", "done"])
        a = adapter(lambda p, n, act: rec(next(outcomes)), asks)
        assert (await a._run_step({"step": "1"}, CTX)).ok and len(asks) == 1
        assert not (await a._run_step({"step": "2"}, CTX)).ok and len(asks) == 1  # step 2 ran under the same approval, and failed
        assert a.plan_ok is None
        assert (await a._run_step({"step": "2"}, CTX)).ok and len(asks) == 2  # the failure made it ask again, with the plan as it stands
        a.plan_ok = None
        a.plan = plan("sha256:b")  # another plan is another question
        a.status = {1: "done", 2: "done"}
        assert await a._approved_plan(CTX, 2, "undo") == (True, "") and len(asks) == 3 and asks[-1][1]["action"] == "undo" and [s["n"] for s in asks[-1][1]["steps"]] == [2, 1]  # newest first
        assert asks[0][1]["plan_hash"] == "sha256:a" and asks[2][1]["plan_hash"] == "sha256:b"
    run(go())


def test_the_same_approved_plan_is_not_asked_twice():
    async def go():
        asks = []
        a = adapter(lambda p, n, act: rec("done"), asks)
        assert (await a._approved_plan(CTX, 1, "run"))[0] and (await a._approved_plan(CTX, 2, "run"))[0]
        assert len(asks) == 1
    run(go())


def test_an_undo_is_one_question_too_newest_step_first(tmp_path):
    async def go():
        cm, c, agent, nid, r = await world(tmp_path, FINISHED, undo_script=UNDO_ALL)
        try:
            await until(lambda: confirm_intent(c, CLAUDE, r) is not None)
            c.harness.decide(CLAUDE, confirm_intent(c, CLAUDE, r)["id"], "approve")
            await asyncio.wait_for(r.task, 90)
            assert r.status == "done", (r.status, r.reason)
            u = c.runs.start_uninstall(nid, "chat:claude-desktop")
            await until(lambda: confirm_intent(c, CLAUDE, u) is not None)
            q = confirm_intent(c, CLAUDE, u)
            assert q["payload"]["chat_choice"] and q["payload"]["action"] == "undo" and q["payload"]["approval_kind"] == "uninstall"
            assert [q["text_fallback"].index(f"{n}. ") for n in (6, 5, 4, 3, 2, 1)] == sorted(q["text_fallback"].index(f"{n}. ") for n in (6, 5, 4, 3, 2, 1))  # step 6 is listed first
            c.harness.decide(CLAUDE, q["id"], "approve")
            await asyncio.wait_for(u.task, 90)
            assert u.status == "rolled_back", (u.status, u.reason)
            assert len([i for i in c.harness.intents(CLAUDE, u.id)["intents"] if i["kind"] == "confirm"]) == 1
        finally:
            await cm.__aexit__(None, None, None)
    run(go())

"""Approvals in the chat (P4, finding M and owner decision 2): the native dialog is ONLY for pairing a computer and storing or removing a key; every other decision is ONE chat question bound to its plan hash,
chosen by DATA (approval-policy.json kind -> gesture), recorded as answered in the chat, with what runs, where and how many calls in its text. The operator's answer is recorded apart and counts for nothing.
SIMULATED-LOCAL: the owner's answers are played by this world."""
from __future__ import annotations

import asyncio
import json

from acceptance_world import build, owner_yes, wait_job
from test_acceptance_run import sc, text

from envrec_coordinator import approval
from envrec_coordinator.harness import CHANNELS

CLAUDE = CHANNELS["claude-desktop"]


def run(coro):
    return asyncio.run(coro)


def test_the_policy_is_the_only_place_that_says_which_decisions_open_a_dialog():
    doc = approval.load()
    dialogs = sorted(k for k, v in doc["kinds"].items() if v == "os_dialog")
    chat = sorted(k for k, v in doc["kinds"].items() if v == "chat_choice")
    assert dialogs == ["access_add", "access_remove", "pairing"]  # owner decision 2
    assert chat == ["admission", "board", "connect", "draft_approve", "install", "measurement", "task_run", "tool_install", "tool_uninstall", "uninstall", "unpair"]


def test_a_kind_decides_the_form_of_the_confirmation_and_nothing_else_does(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            h = c.harness
            for kind, mech in approval.load()["kinds"].items():
                p = h.request_confirmation(None, summary=f"A thing of kind {kind}", plan_hash="sha256:" + (kind.encode().hex() * 8)[:64], command="x", undo="y", kind=kind)
                assert bool(p.intent.payload.get("chat_choice")) == (mech == "chat_choice"), kind
                if mech == "chat_choice":
                    assert p.intent.payload["approval_kind"] == kind and [o["id"] for o in p.intent.payload["options"]] == ["approve", "reject"] and "mas_answer" in p.intent.text_fallback
            plain = h.request_confirmation(None, summary="Unlisted", plan_hash="sha256:" + "cd" * 32, command="x", undo="y")
            assert not plain.intent.payload.get("chat_choice")  # no kind: the mechanisms its channel lists, as before
        finally:
            await teardown()
    run(go())


def test_a_tasks_question_says_what_runs_where_and_how_many_calls_and_the_answer_runs_it(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            r = await host.tool("mas_create_task", text="Summarize: the sky is blue.", task_type="summarize", touches=["none"])
            t = text(r)
            assert "1 call to llamacpp-local" in t and "staying on your computers" in t and "No code check was given" in t and "mas_answer" in t and "mas_request_approval" not in t
            with_check = await host.tool("mas_create_task", text="Summarize: the sky is blue.", task_type="summarize", touches=["none"], check={"kind": "contains", "value": "sky"})
            assert "may try up to 2 more routes" in text(with_check)
            # a system dialog cannot answer it
            bad = await host.tool("mas_request_approval", intent_id=sc(r)["intent_id"])
            assert bad.get("isError") and "answer_in_chat" in json.dumps(bad) or "in the chat" in text(bad)
            assert c.services.jobs[sc(r)["task_id"]].state == "awaiting_approval"
            ans = await owner_yes(host, sc(r)["intent_id"])
            assert sc(ans)["mechanism"] == "chat_choice" and "job id for mas_job" in text(ans)
            done = await wait_job(host, sc(r)["task_id"], timeout=60)
            assert sc(done)["state"] in ("done_unchecked", "done")
            ev = [e.payload for e in c.log.events("owner_confirmation") if e.payload.get("mechanism") == "chat_choice"]
            assert ev and all(p["approved"] and p["channel"] == "claude-desktop" for p in ev) and c.log.events("owner_confirmation")[-1].effective_tag == "OWNER"
        finally:
            await teardown()
    run(go())


def test_rejecting_runs_nothing_and_a_cloud_route_says_it_leaves_the_computers(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            c.services.profile.set("local_first", "any"); c.services.profile.apply(c.services.policy)  # the owner prefers the API: the rule then picks the cloud route
            r = await host.tool("mas_create_task", text="Summarize: the sky is blue.", task_type="summarize", touches=["none"])
            assert "leaving your computers (cloud)" in text(r)
            out = await host.tool("mas_answer", intent_id=sc(r)["intent_id"], option="reject")
            assert sc(out)["state"] == "declined"
            await asyncio.sleep(0.5)
            assert c.services.jobs[sc(r)["task_id"]].state == "declined" and not c.log.events("dispatch")
        finally:
            await teardown()
    run(go())


def test_the_operators_answer_is_recorded_apart_and_approves_nothing(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            r = await host.tool("mas_create_task", text="Summarize: the sky is blue.", task_type="summarize", touches=["none"])
            iid = sc(r)["intent_id"]
            out = c.harness.decide(CLAUDE, iid, "approve", by="claude-code")
            assert out["state"] == "not_counted"
            await asyncio.sleep(0.3)
            assert c.services.jobs[sc(r)["task_id"]].state == "awaiting_approval" and not c.log.events("dispatch")
            rec = [e for e in c.log.events("harness_decision") if e.payload.get("by") == "claude-code"]
            assert rec and rec[0].effective_tag != "OWNER" and rec[0].payload["counted"] is False
            assert not [e for e in c.log.events("owner_confirmation")]  # the owner has not answered
            await owner_yes(host, iid)  # and the owner still can
            assert (await wait_job(host, sc(r)["task_id"], timeout=60))["structuredContent"]["state"] in ("done_unchecked", "done")
        finally:
            await teardown()
    run(go())

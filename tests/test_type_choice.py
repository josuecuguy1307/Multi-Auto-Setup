"""V3: the task-type choice is measured on a fixed gold set and checked by independent picks. A signal that is measured (two picks that disagree, none_fits, a model that cannot answer from the list) asks a second
opinion from the cloud when the owner allowed it; when no two independent answers agree the OWNER is asked ONE question and nobody guesses. SIMULATED-LOCAL: scripted models and the acceptance world."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from acceptance_world import build
from test_owner_profile import CH, answer, pending
from test_research import SEARCH, act, readme, run, serve, settled  # noqa: F401
from test_slots import cloud_calls, owner_answers_cloud_use, says

from envrec.model import ScriptedModel
from envrec_coordinator.slots import Slots, decide, load_slots
from test_acceptance_run import sc, text

GOLD = Path(__file__).parent / "fixtures" / "recommender" / "gold_task_types.json"
GOLD_SHA256 = "cd81c924bfe27b9645d5a868c538977d2b3e6821b86c809c1df5a3f0b73cc5d2"
NAMES = ["code", "data-cleaning", "extraction", "numeric-analysis", "summarize", "text", "none_fits"]


def reply(choice):
    return json.dumps({"choice": choice})


def slots_with(local_replies, *, cloud=None, answer_=("allowed", ""), level="public"):
    """A Slots with a scripted local model and a scripted cloud (a list of choices; None = no route) and the owner's recorded answer given."""
    model = ScriptedModel(list(local_replies))
    cloud_in = list(cloud or [])

    async def remote(prompt, timeout_s):
        if not cloud_in:
            from envrec_coordinator.slots import SlotRefused
            raise SlotRefused("no_route")
        return {"text": reply(cloud_in.pop(0)), "summary": {"model_requested": "cloud-m", "model_returned": "cloud-m-1", "usage": {"prompt_tokens": 5, "completion_tokens": 2}, "response_id": "r", "node_host": "h"}}
    events = []
    s = Slots(load_slots(), local=lambda _n: model, remote=remote if cloud is not None else None, level=level, permit=lambda: answer_, log=lambda t, p: events.append((t, p)))
    return s, model, events


def ctx(order):
    return "Request: x\n" + "\n".join(f"- {n}" for n in order)


def go(coro):
    return asyncio.run(coro)


def test_the_gold_set_is_fixed_and_covers_every_catalog_type_twice():
    raw = GOLD.read_bytes()
    d = json.loads(raw)
    assert len(d["tasks"]) == 12 and [t["id"] for t in d["tasks"]] == [f"g{n:02d}" for n in range(1, 13)]
    by = {}
    for t in d["tasks"]:
        by.setdefault(t["type"], []).append(t["id"])
    assert {k: len(v) for k, v in by.items()} == {"code": 2, "data-cleaning": 2, "extraction": 2, "numeric-analysis": 2, "summarize": 2, "text": 2}
    assert hashlib.sha256(raw).hexdigest() == GOLD_SHA256, "the gold set is never edited to pass: a new set is a new file with a new date"


def test_two_picks_that_agree_settle_it_without_the_cloud():
    s, model, _ = slots_with([reply("summarize")], cloud=["code"])
    d = go(decide(s, "task_type", NAMES, ctx, first="summarize"))
    assert d.resolved == "summarize" and d.signals == [] and d.escalated == "" and len(model.calls) == 1 and cloud_calls_of(s) == 0
    assert model.calls[0][1].index("- none_fits") < model.calls[0][1].index("- code")  # the independent re-pick sees the list in the reverse order


def cloud_calls_of(s):
    return sum(1 for c in s.calls if c.where == "node")


def test_a_disagreement_asks_the_cloud_and_counts_a_pick_two_independent_answers_share():
    s, _, _ = slots_with([reply("text")], cloud=["text"])
    d = go(decide(s, "task_type", NAMES, ctx, first="summarize"))
    assert d.signals == ["picks_disagree"] and d.escalated == "node" and d.resolved == "text" and [w for w, _ in d.picks] == ["local", "local", "node"]
    s2, _, _ = slots_with([reply("text")], cloud=["code"])  # the cloud agrees with neither: nobody settles it
    d2 = go(decide(s2, "task_type", NAMES, ctx, first="summarize"))
    assert d2.resolved is None and d2.options == ["summarize", "text", "code"]
    s3, _, _ = slots_with([reply("text")], cloud=["summarize"])
    assert go(decide(s3, "task_type", NAMES, ctx, first="summarize")).resolved == "summarize"


def test_without_the_cloud_a_disagreement_goes_to_the_owner_with_the_typed_reason():
    s, _, _ = slots_with([reply("text")], cloud=["text"], level="confidential")
    d = go(decide(s, "task_type", NAMES, ctx, first="summarize"))
    assert d.resolved is None and d.options == ["summarize", "text"] and d.escalated == "confidential" and cloud_calls_of(s) == 0
    s2, _, _ = slots_with([reply("text")], cloud=["text"], answer_=(None, "stance_unanswered"))
    assert go(decide(s2, "task_type", NAMES, ctx, first="summarize")).escalated == "stance_unanswered"
    s3, _, _ = slots_with([reply("text")], cloud=["text"], answer_=("never", ""))
    d3 = go(decide(s3, "task_type", NAMES, ctx, first="summarize"))
    assert d3.escalated == "stance_never" and d3.resolved is None


def test_none_fits_is_a_signal_and_the_cloud_can_confirm_it_but_two_local_answers_do_not_outvote_it():
    s, _, _ = slots_with([reply("none_fits")], cloud=["none_fits"])
    d = go(decide(s, "task_type", NAMES, ctx, first="none_fits"))
    assert d.signals == ["none_fits"] and d.resolved == "none_fits" and d.escalated == "node"
    s2, _, _ = slots_with([reply("none_fits")], cloud=["text"])
    d2 = go(decide(s2, "task_type", NAMES, ctx, first="none_fits"))
    assert d2.resolved is None and d2.options == ["none_fits", "text"]
    s3, _, _ = slots_with([reply("none_fits")], cloud=None)  # the cloud cannot be asked and every answer is the same: nothing is left to choose between
    d3 = go(decide(s3, "task_type", NAMES, ctx, first="none_fits"))
    assert d3.resolved == "none_fits" and d3.escalated == "no_route"


def test_a_model_that_cannot_answer_from_the_list_is_itself_a_signal():
    s, _, _ = slots_with(["not json", "still not", "text"], cloud=["text"])
    d = go(decide(s, "task_type", NAMES, ctx, first="text"))
    assert "check_failed" in d.signals and d.escalated == "node" and d.resolved == "text"  # one local pick failed twice; the cloud agrees with the other


# ---- through the real recommender: the owner is asked ONE question when the picks do not settle it ----------------------------------------------------------------------------------

def test_an_unsettled_type_is_one_question_to_the_owner_and_his_answer_goes_on_to_the_recommendation(tmp_path):
    from test_research import GOOD, start
    async def go_():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            owner_answers_cloud_use(c, "never")  # the cloud may not be asked: the disagreement goes to him
            fin = SEARCH + "grant+writing+agent"
            replies = [act("read_known"), act("choose_type", task_type="text", why="Drafting."), reply("summarize"), act("fetch_source", url=fin), act("decline_leads", why="None of the items fits."),
                       act("finish_recommendation", summary="Type unsettled.")]
            r, sent = await start(c, host, replies, pages={fin: GOOD})
            job = await settled(c, sc(r)["job_id"], timeout=30)
            assert job.state == "awaiting_type" and "disagreed (text, summarize)" in job.text and "text:" in job.text and "summarize:" in job.text
            ev = [e.payload for e in c.log.events("type_decision")][-1]
            assert ev["resolved"] is None and ev["options"] == ["text", "summarize"] and ev["escalated"] == "stance_never"
            assert not c.services.tasks and not pending(c, "recommend:")  # nothing was created and the recommendation question is not asked yet
            (q,) = pending(c, "recommend-type:")
            out = answer(c, CH, q, "summarize")
            assert out["ok"] and "Recorded: you chose summarize" in out["next"] and "Recommendation for" in out["next"]
            assert [e.payload["option"] for e in c.log.events("type_answered")] == ["summarize"] and c.log.events("type_answered")[0].effective_tag == "OWNER"
            assert job.state == "awaiting_answer" and pending(c, "recommend:")  # now the recommendation question
        finally:
            await teardown()
    run(go_())

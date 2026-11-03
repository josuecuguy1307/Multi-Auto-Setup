"""The recommender's model slots (V1): a pick from a closed list, a second opinion from the cloud only through the node that holds the access, only on the owner's recorded answer, never for confidential
work, with the model id the backend returned and the tokens recorded on every call. SIMULATED-LOCAL: the real coordinator, node and MCP server of the acceptance world; the cloud API is a mock transport."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from acceptance_world import build
from test_owner_profile import CH, answer, pending

from envrec.contracts import ModelProfile
from envrec.model import OpenAICompatModel
from envrec_coordinator.slots import REFUSALS, SlotRefused, leaks, load_slots, signals

CLOUD = "meta-model-api"


def run(coro):
    return asyncio.run(coro)


def owner_answers_cloud_use(c, option: str):
    """The owner answers the profile question on cloud use in the chat (this world plays him), as the first run does."""
    c.services._question(CH, "cloud_spend")
    answer(c, CH, pending(c, "profile:cloud_spend")[0], option)


def says(w, reply: str):
    w.script.answers.append((lambda env, access, prompt: env == CLOUD and "List:" in prompt, reply))


def cloud_calls(w):
    return [x for x in w.script.calls if x[0] == CLOUD]


# ---- the rules, as data and as pure functions --------------------------------------------------------------------------------------------------------

def test_the_slots_are_data_and_every_refusal_has_a_text():
    d = load_slots()
    assert set(d["slots"]) == {"task_type", "relevance"} and d["slots"]["relevance"]["input"] == "domain_tags_only" and d["slots"]["task_type"]["input"] == "task_text"
    assert d["authority"]["refused_values"] == ["never"] and d["escalation"]["max_calls_per_run"] >= 1
    assert set(d["low_confidence"]) == {"picks_disagree", "none_fits", "check_failed"}
    assert all(REFUSALS[k] for k in ("confidential", "stance_unanswered", "stance_never", "budget", "no_route", "task_text_in_tags_only_slot", "slot_off"))


def test_a_low_confidence_signal_is_measured_never_judged():
    d = load_slots()
    assert signals(["summarize", "summarize"], d) == []
    assert signals(["summarize", "code"], d) == ["picks_disagree"]
    assert signals(["none_fits", "none_fits"], d) == ["none_fits"]
    assert signals(["a", "none_fits"], d) == ["picks_disagree", "none_fits"]
    assert signals(["a"], d, check_failed=True) == ["check_failed"]
    d2 = {**d, "low_confidence": ["picks_disagree"]}  # the signals are the data's: remove one and it never fires
    assert signals(["none_fits"], d2) == []


def test_request_words_are_found_in_an_outbound_prompt_by_a_run_of_words():
    text = "Please summarize the confidential merger terms of Acme and Globex for the board"
    assert leaks(text, "domain words: legal. Acme and Globex merger terms", 4) is False
    assert leaks(text, "context: confidential merger terms of Acme and Globex", 4) is True
    assert leaks(text, "x", 4) is False and leaks("", "anything at all here now", 4) is False


def test_a_local_call_records_the_model_id_the_backend_returned():
    async def go():
        def handler(req):
            return httpx.Response(200, json={"model": "served-as-9b", "choices": [{"message": {"content": "{}"}}], "usage": {"prompt_tokens": 7, "completion_tokens": 2}})
        m = OpenAICompatModel(ModelProfile(name="p", kind="openai_compat", model_id="asked-for", endpoint="http://127.0.0.1:1/v1", supports_tool_calling=False, context_tokens=1000, view_budget_facts=4, menu_max=4),
                              client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        done = await m.complete("s", "u")
        assert done.model_returned == "served-as-9b" and (done.prompt_tokens, done.completion_tokens) == (7, 2)
    run(go())


# ---- the cloud second opinion, through the node ------------------------------------------------------------------------------------------------------

def test_a_second_opinion_goes_through_the_node_with_its_proof_and_only_after_the_owner_answered(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            says(w, '{"choice": "summarize"}')
            s = c.services.new_slots(level="public", task_text="Write a brief about the sky.")
            assert s.refusal("task_type") == "stance_unanswered"  # the profile holds a value, but no OWNER answer to the question exists
            with pytest.raises(SlotRefused) as e:
                await s.second_opinion("task_type", "Request: a brief.", ["summarize", "code", "none_fits"])
            assert e.value.code == "stance_unanswered" and cloud_calls(w) == []
            owner_answers_cloud_use(c, "allowed")
            assert s.refusal("task_type") == ""
            pick = await s.second_opinion("task_type", "Request: a brief.", ["summarize", "code", "none_fits"])
            assert pick.choice == "summarize"
            k = pick.call
            assert (k.where, k.model_requested, k.model_returned, k.prompt_tokens, k.completion_tokens, k.response_id) == ("node", "muse-spark-1.3", "muse-spark-1.3-returned", 17, 5, "chatcmpl-test-0001") and k.node_host
            assert len(cloud_calls(w)) == 1
            ev = [e.payload for e in c.log.events("slot_call")]
            assert ev[-1]["model_returned"] == "muse-spark-1.3-returned" and ev[-1]["usage"] == {"in": 17, "out": 5} and ev[-1]["choice"] == "summarize" and "Request" not in json.dumps(ev[-1])  # the prompt is kept as a hash
            assert any(e.payload.get("routed_task", "").startswith("hj-") for e in c.log.events("task_created"))  # an environment run on the node, like any task
        finally:
            await teardown()
    run(go())


def test_the_owners_answer_never_and_a_later_change_by_a_tool_both_close_it(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            says(w, '{"choice": "summarize"}')
            owner_answers_cloud_use(c, "never")
            s = c.services.new_slots(level="public")
            assert s.refusal("task_type") == "stance_never"
            owner_answers_cloud_use(c, "allowed")
            assert s.refusal("task_type") == ""
            c.services.profile.set("cloud_spend", "never")  # a tool call changes the setting afterwards: that is not an owner event, so the recorded answer no longer matches
            assert s.refusal("task_type") == "stance_unanswered" and cloud_calls(w) == []
        finally:
            await teardown()
    run(go())


def test_confidential_work_never_reaches_the_cloud_and_the_run_budget_is_data(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            says(w, '{"choice": "summarize"}')
            owner_answers_cloud_use(c, "allowed")
            conf = c.services.new_slots(level="confidential", task_text="x")
            assert conf.refusal("task_type") == "confidential"
            with pytest.raises(SlotRefused):
                await conf.second_opinion("task_type", "Request: a brief.", ["summarize", "none_fits"])
            assert cloud_calls(w) == []
            s = c.services.new_slots(level="internal")
            for _ in range(load_slots()["escalation"]["max_calls_per_run"]):
                await s.second_opinion("task_type", "Request: a brief.", ["summarize", "none_fits"])
            assert s.refusal("task_type") == "budget"
            with pytest.raises(SlotRefused) as e:
                await s.second_opinion("task_type", "Request: a brief.", ["summarize", "none_fits"])
            assert e.value.code == "budget" and len(cloud_calls(w)) == load_slots()["escalation"]["max_calls_per_run"]
        finally:
            await teardown()
    run(go())


def test_a_pick_off_the_list_is_refused_and_a_tags_only_slot_refuses_words_of_the_request(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            owner_answers_cloud_use(c, "allowed")
            says(w, '{"choice": "made-up"}')
            s = c.services.new_slots(level="public", task_text="Please review the grant proposal budget for the foundation")
            with pytest.raises(SlotRefused) as e:
                await s.second_opinion("task_type", "Request: a brief.", ["summarize", "none_fits"])
            assert e.value.code == "invalid_pick" and len(cloud_calls(w)) == 2  # one retry with the error, then refused: the code never picks for the model
            before = len(cloud_calls(w))
            with pytest.raises(SlotRefused) as e2:
                await s.second_opinion("relevance", "Domain: please review the grant proposal budget for the foundation", ["1", "none"])
            assert e2.value.code == "task_text_in_tags_only_slot" and len(cloud_calls(w)) == before
            w.script.answers.clear()
            says(w, '{"choice": "1"}')
            ok = await s.second_opinion("relevance", "Domain: grants", ["1", "none"])
            assert ok.choice == "1"
        finally:
            await teardown()
    run(go())


def test_a_second_opinion_task_without_the_owners_answer_is_rejected_by_the_node_desk(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            says(w, '{"choice": "summarize"}')
            s = c.services.new_slots(level="public")
            with pytest.raises(SlotRefused) as e:  # the permit is bypassed on purpose: the desk still finds no owner origin for an hj- task
                await s.ask("task_type", "Request: a brief.", ["summarize", "none_fits"], where="node")
            assert e.value.code == "no_owner_origin" and cloud_calls(w) == []
        finally:
            await teardown()
    run(go())

"""What the first real chat session showed (2026-10-30), as tests: answering by number, the pick that must stay open, options as an unordered set with the numbers to compare them, the machine card on the
map, live activity per machine, Spanish for the owner. SIMULATED-LOCAL: a real coordinator and node, scripted models."""
from __future__ import annotations

import asyncio
import json
import re

import pytest

from envrec.model import ScriptedModel
from envrec_coordinator import harness as H
from envrec_coordinator.harness import CHANNELS, Refused
from envrec_coordinator.runs import describe_event
from envrec_harness import cards, host
from mas_world import coordinator
from test_harness_core import settle
from test_mas_agent import ANALYSIS_SCRIPT, AVAILABLE, analysed, down, node_up, options_run, options_script

CLAUDE = CHANNELS["claude-desktop"]


def run(coro):
    return asyncio.run(coro)


async def finished_options(c, nid):
    await analysed(c, nid)
    holder = {}
    o = await options_run(c, nid, holder, "local")
    return o


def test_an_answer_may_name_the_option_by_id_by_number_or_by_label_and_a_wrong_one_lists_the_valid_ones(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            agent, task, nid = await node_up(c, tmp_path)
            try:
                for given in ("local", "1", "1.", "#1", "Open-source local"):
                    a = await analysed(c, nid) if not c.runs.stored_analysis(nid) else None
                    holder = {}
                    o = c.runs.start_options(nid, "chat:claude-desktop", model=ScriptedModel(options_script(holder)), status=AVAILABLE)
                    holder["run"] = o
                    await settle(lambda: o.adapter is not None and o.adapter.pending is not None)
                    q = next(i for i in c.harness.intents(CLAUDE, o.id)["intents"] if i["kind"] == "ask_choice")
                    assert "1. Open-source local" in q["text_fallback"] and "[id: local]" in q["text_fallback"] and "Reply with the number or the id" in q["text_fallback"]
                    if given == "local":
                        with pytest.raises(Refused) as e:  # a wrong answer says what is valid
                            c.harness.decide(CLAUDE, q["id"], "7")
                        assert e.value.reason == "bad_option" and "1) local" in e.value.message and "2) cli" in e.value.message and "Valid options" in e.value.message
                        with pytest.raises(Refused):
                            c.harness.decide(CLAUDE, q["id"], "")
                    assert c.harness.decide(CLAUDE, q["id"], given)["ok"] is True
                    await asyncio.wait_for(o.task, 60)
                    assert [e for e in o.store.events() if e.type == "owner_answer"][0].payload["option"] == "local"
            finally:
                await down(agent, task)
    run(go())


def test_the_options_run_stays_open_at_the_pick_until_the_owner_answers_and_the_pick_works(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            agent, task, nid = await node_up(c, tmp_path)
            try:
                o = await finished_options(c, nid)
                out = c.harness.intents(CLAUDE, o.id)
                pick = f"{o.id}:pick"
                assert pick in out["waiting"] and out["run"]["ended"] is True and out["run"]["finished"] is False  # ended, but not finished: the owner has not chosen
                q = next(x for x in out["questions"] if x["intent_id"] == pick)
                assert [x["n"] for x in q["options"]] == list(range(1, len(q["options"]) + 1)) and all(x["id"] and x["label"] for x in q["options"])  # the valid ids are in the result
                again = c.harness.intents(CLAUDE, o.id)
                assert again["waiting"] == [pick] and [i.id for i in c.harness._logs[o.id]].count(pick) == 1  # polling again does not tell the end of the run a second time  # asking again changes nothing: it is still open
                r = c.harness.decide(CLAUDE, pick, "2")  # by number
                assert r["ok"] and o.picked_option == q["options"][1]["id"]
                after = c.harness.intents(CLAUDE, o.id)
                assert after["waiting"] == [] and after["run"]["finished"] is True
                ev = [e for e in o.store.events() if e.type == "owner_pick_option"][0]
                assert ev.payload["channel"] == "claude-desktop" and ev.effective_tag == "OWNER"
            finally:
                await down(agent, task)
    run(go())


def test_the_options_are_an_unordered_set_with_every_explanation_the_numbers_and_no_recommendation(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            agent, task, nid = await node_up(c, tmp_path)
            try:
                o = await finished_options(c, nid)
                log = c.harness.intents(CLAUDE, o.id)["intents"]
                data = next(i for i in log if i["id"].endswith(":data:options"))
                opts = data["payload"]["data"]["options"]
                assert [x["id"] for x in opts] == sorted(x["id"] for x in opts)  # ordered by id, never by preference
                assert len(opts) == len([x for x in o.result["options"]]) and all(x.get("explanation") for x in opts)
                assert data["payload"]["data"]["no_recommendation"] is True and "not ordered by preference" in data["text_fallback"] and "the owner chooses" in data["text_fallback"]
                assert all("fit" in x and "budget_gib" in x["fit"] for x in opts)
                for x in opts:
                    assert x["explanation"][:30] in data["text_fallback"]  # all of them are in the text, not one
                html = c.harness.card(CLAUDE, data["id"], "mcp", True)["html"]
                assert html.count("explanation, not verified") == len(opts) and html.count('class="fit"') >= len(opts)
                assert "Installing only happens through Multi Auto Setup" in html and "Installing only happens through Multi Auto Setup's setup flow" in data["text_fallback"]
                texts = "\n".join(i["text_fallback"] for i in log if i["kind"] in ("show_data", "ask_choice") and not i["id"].endswith(":end"))
                assert len(re.findall(r"^1\. ", texts, flags=re.M)) == 1  # the numbered list is printed once per result, not twice
            finally:
                await down(agent, task)
    run(go())


def test_the_map_opens_the_whole_machine_card_of_a_machine_that_was_analysed_and_shows_what_it_does_now(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            agent, task, nid = await node_up(c, tmp_path)
            try:
                await analysed(c, nid)
                d = c.harness.network_data()
                n = d["nodes"][0]
                assert n["machine"]["groups"]["hardware"] and n["machine"]["name"] == "node1"
                assert all(r["value"] != "catalog_top" for g in n["machine"]["groups"].values() for r in g)  # internal names are said in words
                html = cards.network(d)
                assert 'data-for="' + nid in html and 'data-card="machine"' in html.split('data-for="' + nid)[1]  # clicking the machine opens its card, nested in the map
                # while a run is active the machine says what it is doing and who acted
                holder = {}
                o = c.runs.start_options(nid, "chat:claude-desktop", model=ScriptedModel(options_script(holder)), status=AVAILABLE)
                holder["run"] = o
                await settle(lambda: o.adapter is not None and o.adapter.pending is not None)
                live = c.harness.network_data()
                act = live["nodes"][0]["activity"]
                assert live["live"] is True and act["actor"] in ("MODEL", "GUARD", "OWNER") and act["text"] and act["run"] == o.id and live["nodes"][0]["status"] == "busy"
                m = cards.network(live)
                assert "doing now" in m and f'tag {act["actor"]}' in m and 'class="act"' in m
                c.harness.intents(CLAUDE, o.id)  # the chat polls: the question becomes an intent
                c.harness.decide(CLAUDE, f"{o.id}:q:which_ai", "local")
                await asyncio.wait_for(o.task, 60)
            finally:
                await down(agent, task)
    run(go())


def test_the_confidential_lock_is_a_lock_and_amber_is_only_for_the_cloud():
    d = {"nodes": [{"id": "a", "name": "A", "status": "online", "confidential": True}, {"id": "b", "name": "B", "status": "busy", "route": "cloud"}], "clouds": [{"name": "x"}]}
    m = cards.network(d)
    assert "lockl" in m and "lock-badge" in m and "&#9679;" not in m  # no amber dot standing for "confidential"
    css = cards.CSS
    for rule in re.findall(r"[^{}]*lock[^{}]*\{[^}]*\}", css):
        assert "amber" not in rule, rule
    busy = re.search(r"\.st-dot\.busy\{[^}]*\}", css).group(0)
    assert "amber" not in busy
    assert "route-cloud" in m and "amber" in re.search(r"\.mas-link\.route-cloud\{[^}]*\}", css).group(0)  # amber means: it goes to the cloud


def test_what_the_owner_reads_is_in_english():
    from envrec.contracts import Actor, Event
    for t, payload, must in (("action_chosen", {"action": "call_tool", "tool": "node_gpu", "why": "x"}, "chose"), ("tool_run", {"tool": "node_gpu"}, "ran node_gpu on the machine"), ("tool_result", {"tool": "t", "ok": True, "facts": 3}, "3 facts"),
                             ("run_requested", {}, "asked for this run"), ("owner_answer", {"question": "q", "option": "o"}, "answered q: o"), ("owner_pick_option", {"option": "o"}, "picked option o"),
                             ("run_end", {"stop_reason": "protocol_done"}, "run ended"), ("protocol_state", {"from": "a", "to": "b", "by": "evidence"}, "protocol a")):
        text = describe_event(Event(actor=Actor.loop, type=t, payload=payload, run_id="r", tag="GUARD"))
        assert must in text and not re.search(r"[áéíóúñ¿¡]", text), text
    qs = json.loads(open("skills/protocol-setup-options/references/questions.json").read())
    assert "Which AI do you want to use" in qs["which_ai"]["text"] and not any(re.search(r"[áéíóúñ¿¡]", o["label"] + o.get("detail", "")) for o in qs["which_ai"]["options"])


def test_the_mcp_view_refreshes_itself_while_a_run_is_active_and_asks_for_the_inline_display():
    doc = host.document("", "mcp")
    assert "setInterval" in doc and "mas_live" in doc and "every_s" in doc and "ui/request-display-mode" in doc and "mode:'inline'" in doc and "mas_view" in doc
    assert "clearInterval" in doc  # it stops when nothing is active
    assert "slot" in doc  # a refreshed map replaces itself; the other cards stay


def test_the_live_tool_is_the_views_own_and_the_model_is_not_offered_it():
    from envrec_harness.mcp_server import APP_ONLY, TOOLS, McpAdapter
    assert set(APP_ONLY) == {"mas_live", "mas_view"} and not set(APP_ONLY) & set(TOOLS)
    tools = McpAdapter(None).tools_list({})["tools"]
    assert [t["name"] for t in tools if t["_meta"].get("ui", {}).get("visibility") == ["app"]] == ["mas_live", "mas_view"]

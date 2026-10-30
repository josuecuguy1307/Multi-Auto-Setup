"""The harness agent on the coordinator: a MODEL picks read-only probes from a closed menu, code runs them on the node through signed tasks, the OWNER answers closed questions and picks.
SIMULATED-LOCAL: real TLS on loopback and a real node agent in this process; the model is scripted (the real local model is exercised by the verification run)."""
from __future__ import annotations

import asyncio
import json
from collections import Counter

import pytest

from envrec.model import ScriptedModel
from envrec.model.scripted import scripted_profile
from envrec.skills.discover import library
from envrec_coordinator import agent as A
from envrec_coordinator import options as O
from envrec_node.agent import NodeAgent
from mas_world import coordinator, offer, pair_node

CODE = "482915"
AVAILABLE = {"available": True, "profile": "scripted", "reason": ""}


def run(coro):
    return asyncio.run(coro)


def act(tool=None, action="call_tool", **kw):
    d = {"action": action, "why": kw.pop("why", "next step")}
    if tool:
        d["tool"] = tool
    d.update(kw)
    return json.dumps(d)


async def node_up(c, tmp, name="node1"):
    offer(c, CODE, oid=f"o-{name}")
    await pair_node(c, tmp, CODE, node=name)
    agent = NodeAgent(tmp / name, interval_s=0.2, clock=c.clock)
    task = asyncio.create_task(agent.run())
    nid = c.registry.all()[-1].node_id
    for _ in range(300):
        if nid in c._sessions:
            break
        await asyncio.sleep(0.02)
    return agent, task, nid


async def down(agent, task):
    agent.stop_event.set()
    await asyncio.wait_for(task, 5)


async def answer_when_asked(c, run_, qid, option, timeout=20):
    for _ in range(int(timeout / 0.02)):
        p = run_.adapter.pending if run_.adapter else None
        if p is not None and p.qid == qid:
            assert c.answer_run(run_.id, qid, option)
            return p
        await asyncio.sleep(0.02)
    raise AssertionError(f"the question {qid} was never asked")


ANALYSIS_SCRIPT = [act("node_os_details"), act("node_gpu"), act("node_memory_fit"), act("node_runtimes"), act("node_ai_clis"),
                   act("record_finding", args={"text": "A laptop without a discrete GPU: models run on the CPU.", "facts": "machine.os.name, machine.model_path"}),
                   act("finish_analysis", args={"known": "OS, GPU and memory", "unknown": "programs were not read", "why": "not needed for the choice"})]


def options_script(holder, *, owner_choice=True):
    def explain(s, u):
        ad = holder["run"].adapter
        left = [o["id"] for o in ad.options_built if o["id"] not in ad.explained]
        return act("explain_option", args={"option": left[0], "text": "It is listed because it fits this machine, and its limit is the memory."})
    return [act("load_analysis"), act("ask_owner", args={"question": "which_ai"}), act("build_options"), explain, explain, explain, explain]


async def analysed(c, nid, replies=None):
    """Run protocol-analyze to its end with a scripted model; the analysis is stored when it closes."""
    r = c.runs.start_analysis(nid, "web", model=ScriptedModel(list(replies or ANALYSIS_SCRIPT)), status=AVAILABLE)
    await asyncio.wait_for(r.task, 60)
    return r


async def options_run(c, nid, holder, answer="local"):
    r = c.runs.start_options(nid, "web", model=ScriptedModel(options_script(holder)), status=AVAILABLE)
    holder["run"] = r
    await answer_when_asked(c, r, "which_ai", answer)
    await asyncio.wait_for(r.task, 60)
    return r


def test_the_analysis_closes_by_itself_when_the_models_finish_analysis_succeeds_and_it_is_stored(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c") as c:
            agent, task, nid = await node_up(c, tmp_path)
            r = await analysed(c, nid)
            assert r.status == "done", (r.status, r.reason, r.summary)
            counts = c.runs.actor_counts(r)
            assert counts["MODEL"] == 14 and counts["OWNER"] == 1 and counts["SCRIPT"] == 0 and counts["CLAUDE-CODE"] == 0 and counts["GUARD"] > 0, counts  # 7 model calls and 7 chosen actions; OWNER = the request
            assert r.summary["stop_reason"] == "protocol_done" and r.summary["model_calls"] == 7 and r.summary["tools_run"][-1] == "finish_analysis"  # the model was not asked again after the closing tool
            moves = [e for e in r.store.events() if e.type == "protocol_state"]
            assert [(e.payload["from"], e.payload["to"], e.payload["by"], e.payload["closed_by"], e.effective_tag) for e in moves] == [("survey", "done", "evidence", "finish_analysis", "GUARD")]
            stored = c.runs.stored_analysis(nid)
            exit_keys = {n["fact"] for n in [sk for sk in library().protocols() if sk.meta.protocol == "analyze"][0].meta.states.states["survey"].advance[0].needs}
            assert stored and stored["run"] == r.id and exit_keys <= set(stored["facts"]) and {"machine.gpus", "machine.runtimes", "machine.ai_clis", "machine.api_key_variable_names", "machine.vram_gib"} <= exit_keys
            assert stored["facts"]["machine.model_path"]["status"] == "tested" and stored["facts"]["machine.model_path"]["probe"] == "node_memory_fit"
            assert stored["facts"]["analysis.finished"]["status"] == "tested" and stored["facts"]["analysis.summary"]["status"] == "hypothesis"  # the guard's record is not the model's reading
            assert c.log.events("analysis_stored")[0].payload["run"] == r.id
            res = r.result
            assert res["agent"] is True and res["groups"]["system"]["machine.os.name"] and res["findings"][0]["status"] == "hypothesis" and res["findings"][0]["rests_on"] == ["machine.os.name", "machine.model_path"]
            assert res["finished"]["unknown"] == "programs were not read" and res["options"] == []  # which AI is not decided here
            await down(agent, task)
    run(go())


def test_a_finish_analysis_that_the_guard_refuses_closes_nothing_and_stores_nothing(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c") as c:
            agent, task, nid = await node_up(c, tmp_path)
            r = await analysed(c, nid, [act("node_os_details"), act("finish_analysis", args={"known": "the OS"}), act("finish_analysis", args={"known": "the OS"})])
            assert r.status != "done" and c.runs.stored_analysis(nid) is None
            assert not [e for e in r.store.events() if e.type == "protocol_state"]
            assert "finish_analysis" not in r.summary["tools_run"]  # not on the menu until the memory for models is known
            await down(agent, task)
    run(go())


def test_the_options_protocol_cannot_start_without_a_stored_analysis_and_calls_no_model(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c") as c:
            agent, task, nid = await node_up(c, tmp_path)
            model = ScriptedModel([act("load_analysis")])
            r = c.runs.start_options(nid, "web", model=model, status=AVAILABLE)
            assert r.status == "no_analysis" and "analyse it first" in r.reason and r.store is None and r.task is None and model.calls == []
            assert c.runs.start_options("n-nobody", "web").status == "failed"
            await down(agent, task)
    run(go())


def test_the_options_protocol_reads_the_stored_analysis_without_probing_and_closes_when_every_option_is_explained(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c") as c:
            agent, task, nid = await node_up(c, tmp_path)
            a = await analysed(c, nid)
            tasks_before = len(c.tasks.list())
            holder: dict = {}
            r = await options_run(c, nid, holder)
            assert r.status == "done", (r.status, r.reason, r.summary)
            assert len(c.tasks.list()) == tasks_before  # nothing was probed again: no task reached the node
            assert r.summary["tools_run"][:3] == ["load_analysis", "ask_owner", "build_options"] and r.summary["stop_reason"] == "protocol_done"
            loaded = r.store.current_fact(r.target.id, "machine.model_path")
            assert loaded.status.value == "tested" and loaded.evidence[0].provider == f"stored analysis {a.id}" and "not probed again" in loaded.evidence[0].limitations[0]
            counts = c.runs.actor_counts(r)
            assert counts["OWNER"] == 2 and counts["SCRIPT"] == 0 and counts["CLAUDE-CODE"] == 0 and counts["MODEL"] >= 8, counts  # the request and the answer
            moves = [e for e in r.store.events() if e.type == "protocol_state"]
            assert [(e.payload["by"], e.payload["closed_by"], e.effective_tag) for e in moves] == [("evidence", "explain_option", "GUARD")]
            res = r.result
            assert res["answers"] == {"which_ai": "local"} and res["options"] and all(o["explanation_status"] == "hypothesis" and o["status"] == "provisional, not measured here" for o in res["options"])
            facts = {f.key: f.value for f in r.store.current_facts(r.target.id) if f.status.value != "unknown"}
            expected = O.build_options(facts, choice="local")["options"]
            assert [o["id"] for o in res["options"]] == [o["id"] for o in expected][:len(res["options"])]  # the options are the code's, whatever the model said
            await down(agent, task)
    run(go())


def test_no_code_path_advances_either_protocol_for_a_model_that_does_not_finish(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c") as c:
            agent, task, nid = await node_up(c, tmp_path)
            r = await analysed(c, nid, [act("node_os_details"), act("node_memory_fit")] + [act("record_finding", args={"text": "t", "facts": "machine.os.name"})] * 12)
            assert r.status != "done" and c.runs.stored_analysis(nid) is None
            assert not [e for e in r.store.events() if e.type == "protocol_state"]  # all the evidence for the exit but analysis.finished is in, and nobody moved
            await analysed(c, nid)  # a real analysis, so that the options protocol may start
            holder: dict = {}
            m = ScriptedModel([act("load_analysis"), act("ask_owner", args={"question": "which_ai"})] + [act("catalog_lookup")] * 6)
            o = c.runs.start_options(nid, "web", model=m, status=AVAILABLE)
            await answer_when_asked(c, o, "which_ai", "local")
            await asyncio.wait_for(o.task, 60)
            assert o.status != "done" and not [e for e in o.store.events() if e.type == "protocol_state"]  # options were never built or explained, so nothing closed
            await down(agent, task)
    run(go())


def test_the_only_ways_the_loop_moves_a_protocol_are_the_models_advance_the_guards_closing_and_the_dial_that_is_off():
    import ast
    import inspect

    from envrec.loop import run as loop_run
    tree = ast.parse(inspect.getsource(loop_run))
    moves = [(n.args[1].value, n.lineno) for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "move" and len(n.args) == 2 and isinstance(n.args[1], ast.Constant)]
    assert sorted(v for v, _ in moves) == ["code", "evidence", "model"], moves
    walk = [ln for v, ln in moves if v == "code"][0]
    src = inspect.getsource(loop_run).splitlines()
    assert "walk" in " ".join(src[walk - 6:walk]) and "walk_for_model" in inspect.getsource(loop_run)  # "code" moves only under the dial, which the harness profiles leave off


def test_the_menu_is_closed_and_a_tool_outside_the_protocol_step_is_refused_and_counted(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c") as c:
            agent, task, nid = await node_up(c, tmp_path)
            holder: dict = {}
            model = ScriptedModel([act("finish_analysis", args={"known": "x"}), act("build_options"), act("rm_rf"), act("node_os_details"), act("node_memory_fit"), act(action="advance", to="conclude"),
                                   act(action="advance", to="done")])
            r = c.runs.start_analysis(nid, "web", model=model, status=AVAILABLE)
            await asyncio.wait_for(r.task, 40)
            assert r.summary["rejected_actions"] >= 2 and r.status in ("stopped", "blocked_needs_human")
            tools_run = r.summary["tools_run"]
            assert "finish_analysis" not in tools_run and "build_options" not in tools_run and "rm_rf" not in tools_run  # none of them ran
            await down(agent, task)
    run(go())


def test_repeated_invalid_actions_end_the_run_as_blocked_needs_human(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c") as c:
            agent, task, nid = await node_up(c, tmp_path)
            r = c.runs.start_analysis(nid, "web", model=ScriptedModel(["not json at all"] * 30), status=AVAILABLE)
            await asyncio.wait_for(r.task, 40)
            assert r.status == "blocked_needs_human" and r.summary["stop_reason"] == "blocked_needs_human" and r.result["agent"] is True
            counts = c.runs.actor_counts(r)
            assert counts["SCRIPT"] >= 3 and counts["CLAUDE-CODE"] == 0  # the fallbacks are code's, and said so (SCRIPT)
            assert not [e for e in r.store.events() if e.type == "action_chosen" and e.effective_tag == "MODEL"]  # no action was ever chosen by the model
            await down(agent, task)
    run(go())


def test_with_no_model_the_run_says_so_and_the_raw_report_is_labeled_code_only_and_never_an_agent_analysis(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c") as c:  # no model profile configured
            agent, task, nid = await node_up(c, tmp_path)
            assert c.runs.model_status() == {"available": False, "profile": None, "reason": "no model is configured on the coordinator (start it with --model-profile NAME)"}
            r = c.runs.start_analysis(nid, "web")
            assert r.status == "no_model" and r.reason.startswith("no model available on the coordinator")
            await asyncio.wait_for(r.task, 60)
            assert r.result["agent"] is False and r.result["label"].startswith("code-only, no model") and r.result["groups"]["system"]["machine.os.name"] and r.result["findings"] == [] and r.result["options"] == []
            counts = c.runs.actor_counts(r)
            assert counts["MODEL"] == 0 and counts["SCRIPT"] >= 3  # the probes were run by code in a fixed order, and tagged SCRIPT
            assert c.runs.view(r.id)["status"] == "no_model"
            await down(agent, task)
    run(go())


def test_an_unreachable_model_is_a_typed_reason(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c") as c:
            c.runs.model_profile = "muse-glimmer"
            c.runs.models_file = tmp_path / "models.yaml"
            c.runs.models_file.write_text("profiles:\n  muse-glimmer: {name: muse-glimmer, kind: openai_compat, model_id: m, endpoint: 'http://127.0.0.1:9/v1', supports_tool_calling: false, context_tokens: 100, view_budget_facts: 1, menu_max: 2}\n")
            st = c.runs.model_status()
            assert st["available"] is False and "did not answer" in st["reason"]
            agent, task, nid = await node_up(c, tmp_path)
            r = c.runs.start_analysis(nid, "web")
            assert r.status == "no_model" and "did not answer" in r.reason
            await asyncio.wait_for(r.task, 60)
            await down(agent, task)
    run(go())


def test_the_owner_pressing_the_button_is_an_owner_event_and_an_offline_machine_is_refused_plainly(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c") as c:
            r = c.runs.start_analysis("n-nobody", "web")
            assert r.status == "failed" and "not paired" in r.reason
            ev = [e for e in c.log.events("analysis_requested")]
            assert ev and ev[0].effective_tag == "OWNER" and ev[0].payload["via"] == "web"
    run(go())


def test_ask_owner_offers_only_the_closed_questions_and_closed_options_and_a_wrong_option_is_not_taken(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c") as c:
            agent, task, nid = await node_up(c, tmp_path)
            await analysed(c, nid)
            model = ScriptedModel([act("load_analysis"), act("ask_owner", args={"question": "free text from the model"}), act("ask_owner", args={"question": "sensitivity"}), act("catalog_lookup")])
            r = c.runs.start_options(nid, "web", model=model, status=AVAILABLE)
            for _ in range(500):
                if r.adapter.pending:
                    break
                await asyncio.sleep(0.02)
            assert r.adapter.pending.qid == "sensitivity" and [o["id"] for o in r.adapter.pending.options] == ["public", "internal", "confidential"]
            assert c.answer_run(r.id, "sensitivity", "everything") is False and c.answer_run(r.id, "which_ai", "local") is False  # not an option / not the question asked
            assert c.answer_run(r.id, "sensitivity", "confidential") is True
            assert c.answer_run(r.id, "sensitivity", "public") is False  # answered once
            await asyncio.wait_for(r.task, 40)
            owner = [e for e in r.store.events() if e.type == "owner_answer"]
            assert len(owner) == 1 and owner[0].effective_tag == "OWNER" and owner[0].payload == {"question": "sensitivity", "option": "confidential"}
            assert any(e.type == "tool_result" and e.payload.get("ok") is False and "unknown question" in str(e.payload.get("error")) for e in r.store.events())
            await down(agent, task)
    run(go())


def test_a_finding_must_rest_on_facts_that_exist(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c") as c:
            agent, task, nid = await node_up(c, tmp_path)
            model = ScriptedModel([act("node_os_details"), act("record_finding", args={"text": "It is fast.", "facts": ["machine.speed"]}), act("record_finding", args={"text": "x", "facts": []})])
            r = c.runs.start_analysis(nid, "web", model=model, status=AVAILABLE)
            await asyncio.wait_for(r.task, 40)
            assert r.result["findings"] == []  # neither was recorded
            errs = [str(e.payload.get("error")) for e in r.store.events() if e.type == "tool_result" and e.payload.get("ok") is False]
            assert any("do not exist yet" in x for x in errs) and any("needs text and the fact keys" in x for x in errs)
            await down(agent, task)
    run(go())


def test_build_options_is_not_on_the_menu_before_the_analysis_is_loaded_and_is_refused_before_the_owner_chose(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c") as c:
            agent, task, nid = await node_up(c, tmp_path)
            await analysed(c, nid)
            model = ScriptedModel([act("build_options"), act("load_analysis"), act("build_options")])
            r = c.runs.start_options(nid, "web", model=model, status=AVAILABLE)
            await asyncio.wait_for(r.task, 40)
            errs = [str(e.payload.get("error")) for e in r.store.events() if e.type == "tool_result" and e.payload.get("ok") is False]
            assert any("ask the owner which_ai first" in x for x in errs) and r.result["options"] == []
            assert [e.payload["tool"] for e in r.store.events() if e.type == "tool_run"] == ["load_analysis", "build_options"]  # the first build_options was off the menu
            assert any(e.type == "action_rejected" and "not on the menu" in e.payload["reason"] for e in r.store.events())
            await down(agent, task)
    run(go())


def test_the_owner_picks_an_option_and_it_is_recorded_as_an_owner_event_and_nothing_is_installed(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c") as c:
            agent, task, nid = await node_up(c, tmp_path)
            await analysed(c, nid)
            holder: dict = {}
            r = await options_run(c, nid, holder)
            first = r.result["options"][0]["id"]
            assert c.pick_option(r.id, "not-an-option") is False and c.pick_option(r.id, first) is True
            assert r.picked_option == first
            ev = [e for e in r.store.events() if e.type == "owner_pick_option"]
            assert ev and ev[0].effective_tag == "OWNER" and ev[0].payload == {"option": first}
            assert [t.task_type for t in c.tasks.list()] and all(t.task_type in ("node_report", *A.__dict__["T"].PROBES) for t in c.tasks.list())  # only read-only task types ever ran
            await down(agent, task)
    run(go())


def test_no_secret_value_reaches_a_fact_an_event_or_the_run_view(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-never-shown-in-anything-123456")

    async def go():
        async with coordinator(tmp_path / "c") as c:
            agent, task, nid = await node_up(c, tmp_path)
            r = c.runs.start_analysis(nid, "web", model=ScriptedModel([act("node_ai_clis"), act("node_runtimes")], profile=scripted_profile(menu_max=12)), status=AVAILABLE)
            await asyncio.wait_for(r.task, 40)
            blob = json.dumps(c.runs.view(r.id), default=str) + json.dumps([e.payload for e in r.store.events()], default=str) + json.dumps([e.payload for e in c.log.events()], default=str)
            assert "sk-never-shown" not in blob
            names = r.result["groups"]["ai"].get("machine.api_key_variable_names", [])
            assert {"provider": "openai", "name": "OPENAI_API_KEY"} in names  # the NAME is there
            await down(agent, task)
    run(go())


def test_every_need_of_the_analysis_exit_is_at_tested_or_better_and_nothing_was_lowered():
    survey = [sk for sk in library().protocols() if sk.meta.protocol == "analyze"][0].meta.states.states["survey"]
    needs = survey.advance[0].needs
    assert all(n["at_least"] == "tested" for n in needs) and "analysis.finished" in {n["fact"] for n in needs}
    assert {"machine.os.name", "machine.model_path", "machine.gpus", "machine.runtimes", "machine.ai_clis", "machine.api_key_variable_names"} <= {n["fact"] for n in needs}  # what setup-options reads


def test_an_answer_the_operator_gives_for_the_owner_is_tagged_claude_code_recorded_as_such_and_never_counted_as_the_owners(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c") as c:
            agent, task, nid = await node_up(c, tmp_path)
            await analysed(c, nid)
            holder: dict = {}
            r = c.runs.start_options(nid, "claude-code", model=ScriptedModel(options_script(holder)), status=AVAILABLE)
            holder["run"] = r
            for _ in range(500):
                if r.adapter.pending:
                    break
                await asyncio.sleep(0.02)
            assert c.answer_run(r.id, "which_ai", "local", "claude-code") is True
            await asyncio.wait_for(r.task, 60)
            assert r.status == "done", (r.status, r.reason)
            ans = [e for e in r.store.events() if e.type == "owner_answer"]
            assert len(ans) == 1 and ans[0].effective_tag == "CLAUDE-CODE" and ans[0].payload == {"question": "which_ai", "option": "local", "by": "claude-code"}
            counts = c.runs.actor_counts(r)
            assert counts["OWNER"] == 0 and counts["CLAUDE-CODE"] == 2, counts  # the start and the answer, both the operator's
            fact = r.store.current_fact(r.target.id, "owner.answer.which_ai")
            assert fact.evidence[0].provider == "operator (claude-code) for the owner" and "did not answer himself" in fact.evidence[0].limitations[0]
            await down(agent, task)
    run(go())


def test_an_analysis_stored_under_a_narrower_spec_does_not_let_the_options_protocol_start(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c") as c:
            agent, task, nid = await node_up(c, tmp_path)
            c.runs.analyses.mkdir(parents=True, exist_ok=True)
            narrow = {"run": "old", "node": nid, "stored_at": 1.0, "facts": {k: {"value": 1, "status": "tested", "probe": "p"} for k in ("machine.os.name", "machine.model_path", "analysis.finished")}}
            (c.runs.analyses / f"{nid}.json").write_text(json.dumps(narrow))
            assert c.runs.stored_analysis(nid) is None
            assert c.runs.start_options(nid, "web", model=ScriptedModel([]), status=AVAILABLE).status == "no_analysis"
            await down(agent, task)
    run(go())


def test_the_model_can_see_which_option_ids_explain_option_takes_and_the_list_shrinks_as_they_are_explained(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c") as c:
            agent, task, nid = await node_up(c, tmp_path)
            await analysed(c, nid)
            holder: dict = {}
            m = ScriptedModel(options_script(holder))
            r = c.runs.start_options(nid, "web", model=m, status=AVAILABLE)
            holder["run"] = r
            await answer_when_asked(c, r, "which_ai", "local")
            await asyncio.wait_for(r.task, 60)
            ids = [o["id"] for o in r.adapter.options_built]
            seen = [u for _s, u in m.calls if "explain_option(option, text)" in u]
            assert seen and f"option (required, string): one of: {', '.join(ids)} (the options not explained yet)" in seen[0]
            if len(ids) > 1:
                assert f"one of: {', '.join(ids[1:])} (the options not explained yet)" in seen[1]
            await down(agent, task)
    run(go())

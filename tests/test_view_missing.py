"""What the model SEES when an exit lacks a fact: the missing key, the tool that produces it, a typed `done` refusal, a menu cap that keeps that tool in, and the arguments of every tool.
Code changes none of what the model DOES: it never runs a probe for the model and never moves the protocol (the tests that prove it drive a scripted model that only says `done`)."""
from __future__ import annotations

import asyncio
import json

import pytest

from envrec.adapters import Adapter
from envrec.contracts import Actor, Event, Evidence, Fact, FactStatus, ProbeResult, Target, TargetKind, Tier, ToolSpec
from envrec.loop import RunConfig
from envrec.loop.menu import build_menu, render_menu
from envrec.model import ScriptedModel
from envrec.model.scripted import scripted_profile
from envrec.protocols import ProtocolSet
from envrec.skills.discover import library
from envrec.store import Store
from envrec_coordinator import agent as A
from envrec_coordinator.runs import HarnessLoop
from envrec_os import probes

LIB = library()
PS = ProtocolSet.from_library(LIB)


class Rec:
    """What the coordinator's task runner hands back: a finished node task with the typed result of a probe."""

    def __init__(self, result: dict) -> None:
        self.status, self.reason, self.result = "done", "", result
        self.done = asyncio.Event()
        self.done.set()


def act(tool=None, action="call_tool", **kw):
    d = {"action": action, "why": "next"}
    if tool:
        d["tool"] = tool
    d.update(kw)
    return json.dumps(d)


FAKE_RESULTS = {"os_details": probes.os_details, "memory_for_models": probes.memory_for_models, "gpus": probes.gpus, "model_runtimes": probes.model_runtimes, "ai_clis": probes.ai_clis}
ALL_PROBES = ["node_os_details", "node_gpu", "node_memory_fit", "node_runtimes", "node_ai_clis"]


def make(tmp_path, replies, *, menu_max=6):
    store = Store(tmp_path / "s")
    target = A.new_target(store, TargetKind.coordinator, "node-1")
    asked: list[str] = []

    def run_task(task_type, params):
        asked.append(task_type)
        return Rec(FAKE_RESULTS[task_type]())

    adapter = A.AnalysisAdapter(node_id="node-1", node_name="node-1", run_task=run_task, owner_events=A.write_owner_event)
    profile = scripted_profile(menu_max=menu_max)
    model = ScriptedModel(replies, profile)
    loop = HarnessLoop(store, [adapter], model, config=RunConfig(max_steps=20, protocols=PS, library=LIB))
    adapter.run_id = loop.run_id
    return store, target, adapter, model, loop, asked


def go(loop, target):
    return asyncio.run(loop.run(target))


def view_of(model, n):
    return model.calls[n][1]


# ---- the view names the missing key and the tool that makes it ---------------------------------------------------------------------------------------------------------

def test_the_view_names_each_missing_key_and_the_tool_that_produces_it_and_the_line_changes_as_keys_arrive(tmp_path):
    store, target, adapter, model, loop, asked = make(tmp_path, [act("node_os_details"), "x", "x", "x"])
    go(loop, target)
    first, second = view_of(model, 0), view_of(model, 1)
    line = [l for l in first.splitlines() if l.startswith("STILL MISSING:")][0]
    assert line.startswith("STILL MISSING: to move on to done it needs machine.os.name (produced by node_os_details, on the menu now); machine.os.arch (produced by node_os_details, on the menu now);")
    for part in ("machine.model_path (produced by node_memory_fit, on the menu now)", "machine.gpus (produced by node_gpu, on the menu now)", "machine.runtimes (produced by node_runtimes, on the menu now)",
                 "machine.ai_clis (produced by node_ai_clis, on the menu now)", "machine.api_key_variable_names (produced by node_ai_clis, on the menu now)"):
        assert part in line, part
    assert line.endswith("analysis.finished (produced by finish_analysis, not on the menu now)")  # finish_analysis is said not to be offered yet: the rest of the exit is not in
    line2 = [l for l in second.splitlines() if l.startswith("STILL MISSING:")][0]
    assert "machine.os.name" not in line2 and "machine.os.arch" not in line2 and "machine.model_path (produced by node_memory_fit" in line2
    assert "WAY FORWARD" not in first and "WAY FORWARD" not in second  # nothing is open yet: the line is not the way forward


def test_with_the_probes_in_only_the_closing_tool_is_missing_and_it_is_now_on_the_menu(tmp_path):
    store, target, adapter, model, loop, asked = make(tmp_path, [act(t) for t in ALL_PROBES] + ["x", "x", "x"])
    go(loop, target)
    last = view_of(model, 5)
    assert "STILL MISSING: to move on to done it needs analysis.finished (produced by finish_analysis, on the menu now)" in last and "WAY FORWARD" not in last
    assert store.events(type="view_built")[5].payload["menu"][0] == "finish_analysis"  # a producer of the missing key leads the capped menu
    for i in range(5):  # and until the rest of the exit is in the closing tool is not offered at all
        assert "finish_analysis" not in store.events(type="view_built")[i].payload["menu"]


def test_a_closing_tool_is_offered_only_once_the_rest_of_an_exit_is_in_and_this_is_read_from_the_spec():
    from envrec.protocols import EvidenceView, ProtocolRun
    run = ProtocolRun(next(sk for sk in LIB.protocols() if sk.meta.protocol == "analyze"))
    fact = lambda k: Fact(target_id="t", key=k, value=1, status=FactStatus.tested, evidence=[Evidence(provider="p", probe="p", confidence=1.0)])
    need = [n["fact"] for n in run.current.advance[0].needs]
    assert not run.closing_open("finish_analysis", ["analysis.finished"], EvidenceView(facts={}))
    assert run.closing_open("node_gpu", ["machine.gpus"], EvidenceView(facts={}))  # a tool that does not close the step is not affected
    nearly = EvidenceView(facts={k: fact(k) for k in need if k != "analysis.finished"})
    assert run.closing_open("finish_analysis", ["analysis.finished"], nearly)
    assert not run.closing_open("finish_analysis", ["analysis.finished"], EvidenceView(facts={k: fact(k) for k in need if k not in ("analysis.finished", "machine.gpus")}))


def test_a_key_no_registered_tool_makes_is_said_so_instead_of_naming_a_tool(tmp_path):
    class Lone(Adapter):
        name = "lone"

        def tool_specs(self):
            return [ToolSpec(name="read_nothing", ring=0, tier=Tier.observe, target_kinds=[TargetKind.coordinator], one_line="x", source="t")]

        async def run(self, tool, args, ctx):
            return ProbeResult(ok=True)

    store = Store(tmp_path / "s")
    target = A.new_target(store, TargetKind.coordinator, "n")
    model = ScriptedModel(["x", "x", "x"], scripted_profile())
    loop = HarnessLoop(store, [Lone()], model, config=RunConfig(max_steps=3, protocols=PS, library=LIB))
    go(loop, target)
    assert "machine.os.name (no tool for this target produces it)" in view_of(model, 0)


# ---- the declared keys are the real ones --------------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("tool,fn", [("node_os_details", probes.os_details), ("node_gpu", probes.gpus), ("node_memory_fit", probes.memory_for_models), ("node_runtimes", probes.model_runtimes),
                                    ("node_ai_clis", probes.ai_clis), ("node_programs", probes.installed_programs)])
def test_the_keys_a_probe_tool_declares_are_exactly_the_keys_its_node_result_carries(tool, fn):
    assert sorted(fn()["facts"]) == sorted(A.PROBE_PRODUCES[tool]), tool


def test_every_probe_tool_of_the_analysis_declares_what_it_produces():
    specs = {s.name: s for s in A.AnalysisAdapter(node_id="n", node_name="n", run_task=None, owner_events=None).tool_specs()}
    assert all(specs[t].produces == A.PROBE_PRODUCES[t] for t in A.PROBE_TOOLS)


# ---- the menu cap keeps the tool that makes a missing key -------------------------------------------------------------------------------------------------------

def _spec(name, produces=()):
    return ToolSpec(name=name, ring=0, tier=Tier.observe, target_kinds=[TargetKind.host], one_line=f"{name} does a thing", source="fake", produces=list(produces))


class Fake(Adapter):
    name = "fake"

    def __init__(self, specs):
        self.specs = specs

    def tool_specs(self):
        return self.specs

    async def run(self, tool, args, ctx):
        return ProbeResult(ok=True)


def test_a_capped_menu_keeps_the_tool_that_makes_a_missing_key_and_without_the_key_the_order_is_unchanged(tmp_path):
    with Store(tmp_path / "s") as store:
        host = store.put_target(Target(kind=TargetKind.host, ring=0, handle="h"))
        a = Fake([_spec("first"), _spec("second"), _spec("makes_key", ["k.x"]), _spec("last")])
        assert [e.name for e in build_menu([a], host, store, 2)] == ["first", "second"]  # the cap drops the tool that matters
        assert [e.name for e in build_menu([a], host, store, 2, missing={"k.x"})] == ["makes_key", "first"]  # a stable reorder: the rest keep their order
        assert [e.name for e in build_menu([a], host, store, 4, missing={"k.x"})] == ["makes_key", "first", "second", "last"]  # nothing is added or removed


def test_in_the_analysis_the_producers_of_missing_keys_stay_in_a_two_tool_menu(tmp_path):
    store, target, adapter, model, loop, asked = make(tmp_path, [act("node_os_details"), "x", "x", "x"], menu_max=2)
    go(loop, target)
    menus = [e.payload["menu"] for e in store.events(type="view_built")]
    assert menus[0] == ["node_os_details", "node_memory_fit"]  # producers of missing keys lead, in the tools' own order
    assert menus[1] == ["node_memory_fit", "node_gpu"]  # os_details ran; record_finding produces nothing the exit needs, so it is behind the producers


def test_explain_option_is_not_offered_before_the_options_exist_and_stays_after_one_explanation(tmp_path):
    with Store(tmp_path / "s") as store:
        t = A.new_target(store, TargetKind.setup, "n")
        ad = A.OptionsAdapter(node_id="n", node_name="n", load_stored=lambda: None, owner_events=None)
        assert "explain_option" not in [e.name for e in build_menu([ad], t, store, 20)] and "build_options" not in [e.name for e in build_menu([ad], t, store, 20)]
        store.write_fact(Fact(target_id=t.id, key="option.set", value={}, status=FactStatus.tested, evidence=[Evidence(provider="p", probe="p", confidence=1.0)]))
        store.write_fact(Fact(target_id=t.id, key="analysis.loaded", value={}, status=FactStatus.tested, evidence=[Evidence(provider="p", probe="p", confidence=1.0)]))
        store.append_event(Event(actor=Actor.loop, type="tool_result", payload={"tool": "explain_option", "target": t.id, "ok": True}))
        names = [e.name for e in build_menu([ad], t, store, 20)]
        assert "explain_option" in names and "build_options" in names  # one explanation per option: it has to stay on the menu after a success


# ---- a typed refusal of `done` ---------------------------------------------------------------------------------------------------------------------------------

def test_the_refusal_of_done_is_typed_names_the_missing_key_and_reaches_the_next_view(tmp_path):
    store, target, adapter, model, loop, asked = make(tmp_path, [act("node_os_details"), act(action="done"), "x", "x", "x"])
    go(loop, target)
    e = [x for x in store.events(type="protocol_refused") if x.payload["action"] == "done"][0].payload
    assert e["code"] == "protocol_not_ended"
    assert e["missing"] == ["machine.model_path", "machine.ram_total_gib", "machine.vram_gib", "machine.disk_free_gib", "machine.gpus", "machine.runtimes", "machine.ai_clis",
                            "machine.api_key_variable_names", "analysis.finished"]
    assert e["produced_by"]["machine.model_path"] == ["node_memory_fit"] and e["produced_by"]["machine.gpus"] == ["node_gpu"] and e["produced_by"]["analysis.finished"] == ["finish_analysis"]
    assert e["reason"].startswith("the protocol has not ended; still missing: to move on to done it needs machine.model_path (produced by node_memory_fit, on the menu now);")
    assert "Still missing: to move on to done it needs machine.model_path (produced by node_memory_fit, on the menu now);" in view_of(model, 2)


# ---- required arguments are visible ----------------------------------------------------------------------------------------------------------------------------

def test_the_menu_text_shows_every_required_argument_with_what_to_put_in_it(tmp_path):
    store, target, adapter, model, loop, asked = make(tmp_path, ["x", "x", "x"], menu_max=12)
    go(loop, target)
    v = view_of(model, 0)
    assert "record_finding(text, facts)" in v and "text (required, string): what you conclude, one sentence" in v and "facts (required, string): the fact keys it rests on, comma separated" in v
    assert "ask_owner" not in v  # the owner's questions belong to protocol-setup-options
    ad = A.OptionsAdapter(node_id="n", node_name="n", load_stored=lambda: None, owner_events=None)
    from envrec.loop.menu import MenuEntry
    text = render_menu([MenuEntry(s.name, s.one_line, s, ad) for s in ad.tool_specs() if s.name in ("ask_owner", "explain_option")])
    assert "ask_owner(question)" in text and "question (required, string): one of: sensitivity, which_ai, which_api_provider, which_cli" in text and "explain_option(option, text)" in text


# ---- code never does the model's part -------------------------------------------------------------------------------------------------------------------------

def test_a_model_that_only_says_done_gets_no_probe_run_and_no_step_moved_for_it(tmp_path):
    store, target, adapter, model, loop, asked = make(tmp_path, [act(action="done")] * 6)
    s = go(loop, target)
    assert asked == [] and s.tools_run == [] and adapter.findings == 0  # no probe ran: the node was never asked anything
    assert s.stop_reason == "refused_done_loop" and loop._proto_runs[target.id].state == "survey"
    assert not store.events(type="protocol_state") and not [e for e in store.events(type="tool_run")]
    assert not [f for f in store.current_facts(target.id) if f.key.startswith("machine.")]  # no machine fact exists: nobody wrote one
    assert all(e.effective_tag != "SCRIPT" for e in store.events() if e.type == "action_chosen")  # nothing was chosen by code


def test_a_model_that_never_asks_for_the_missing_probe_never_gets_it_and_the_run_ends_typed_not_as_success(tmp_path):
    same = act("record_finding", args={"text": "t", "facts": "machine.os.name"})
    store, target, adapter, model, loop, asked = make(tmp_path, [act("node_os_details"), same, same, same, same, same, same, same, same])
    s = go(loop, target)
    assert asked == ["os_details"] and "node_memory_fit" not in s.tools_run  # only what the model chose ran
    assert s.stop_reason in ("no_progress", "blocked_needs_human") and loop._proto_runs[target.id].state == "survey"


def test_invalid_replies_still_end_the_run_as_blocked_needs_human(tmp_path):
    store, target, adapter, model, loop, asked = make(tmp_path, ["not json"] * 30)
    s = go(loop, target)
    assert s.stop_reason == "blocked_needs_human" and asked == []


# ---- the way forward is the last thing before the instruction, and a refusal says where it is ------------------------------------------------------------------

def test_the_way_forward_line_stays_above_the_menu_where_it_always_was(tmp_path):
    store, host, model, loop = host_run(tmp_path, call3() + ["x", "x", "x"])
    v = view_of(model, 3)
    assert v.index("LAST RESULT") < v.index("WAY FORWARD") < v.index("MENU") < v.index("Reply with one Action as JSON.")
    assert "THIS STEP'S EVIDENCE IS IN" not in v and "STILL MISSING" not in v  # no extra persuasion line: the view states the way forward and nothing more


def test_a_refused_repeat_while_the_way_forward_is_open_tells_the_next_view_where_it_is_and_still_does_not_move_for_the_model(tmp_path):
    same = act("read_os_native", args={"q": "x"})
    store, host, model, loop = host_run(tmp_path, call3() + [same, same, "x", "x", "x"])
    rejected = view_of(model, 5)  # the view after the repeat was refused
    assert "LAST RESULT: rejected: read_os_native was already asked with these arguments; its answer is in the facts. WAY FORWARD (this step's evidence is in): " in rejected
    assert loop._proto_runs[host.id].state == "basics" and not store.events(type="protocol_state")  # the model was told; nobody advanced


def test_the_options_menu_offers_only_the_questions_that_belong_to_it():
    specs = {s.name: s for s in A.OptionsAdapter(node_id="n", node_name="n", load_stored=lambda: None, owner_events=None).tool_specs()}
    text = specs["ask_owner"].input_model["properties"]["question"]["description"]
    assert text == "one of: sensitivity, which_ai, which_api_provider, which_cli" and "pick_waiting_machine" not in text


def test_refusing_done_with_the_exit_open_says_so_and_names_the_way_forward(tmp_path):
    store, host, model, loop = host_run(tmp_path, call3() + [act(action="done"), "x", "x", "x"])
    e = [x for x in store.events(type="protocol_refused") if x.payload["action"] == "done"][0].payload
    assert e["code"] == "protocol_not_ended" and e["forward"] == ["context"] and e["missing"] == []
    assert e["reason"] == "the protocol has not ended; this step's evidence is in, the way forward is to advance to context"
    assert 'LAST RESULT: refused: done is not available in step basics; the protocol has not ended. WAY FORWARD (this step\'s evidence is in): {"action":"advance","to":"context"}' in view_of(model, 4)
    assert loop._proto_runs[host.id].state == "basics"  # told, not moved


def test_the_log_keeps_the_exact_text_the_model_was_shown(tmp_path):
    store, target, adapter, model, loop, asked = make(tmp_path, [act("node_os_details"), "x", "x", "x"])
    go(loop, target)
    shown = [e.payload["view"] for e in store.events(type="view_built")]
    assert shown[0] == view_of(model, 0) and shown[1] == view_of(model, 1)  # what the model got, byte for byte


# ---- a way forward, in a generic protocol: the analysis closes by itself, so it never shows one ---------------------------------------------------------------------

class HostFake(Adapter):
    name = "hostfake"

    def tool_specs(self):
        asking = _spec("read_os_native").model_copy(update={"input_model": {"type": "object", "properties": {"q": {"type": "string", "description": "what to ask"}}, "required": ["q"]}})
        return [_spec("read_os", ["os.name"]), _spec("read_cpu_ram", ["hw.ram_total_gib"]), _spec("read_disks", ["hw.disk_total_gib"]), asking]

    async def run(self, tool, args, ctx):
        s = next(x for x in self.tool_specs() if x.name == tool)
        return ProbeResult(ok=True, facts=[Fact(target_id=ctx.target.id, key=k, value="v", status=FactStatus.tested, evidence=[Evidence(provider="fake", probe=tool, event_id=ctx.event_id, confidence=0.9)])
                                           for k in s.produces])


def host_run(tmp_path, replies):
    store = Store(tmp_path / "h")
    host = store.put_target(Target(kind=TargetKind.host, ring=0, handle="this-machine"))
    model = ScriptedModel(replies, scripted_profile())
    loop = HarnessLoop(store, [HostFake()], model, config=RunConfig(max_steps=20, protocols=PS, library=LIB))
    asyncio.run(loop.run(host))
    return store, host, model, loop


def call3():
    return [act("read_os"), act("read_cpu_ram"), act("read_disks")]


def test_a_fact_the_human_stated_is_shown_first_and_is_never_pushed_out_by_the_view_budget():
    from envrec.contracts import CreatedBy
    from envrec.loop.view import rank_facts
    ev = [Evidence(provider="p", probe="p", confidence=1.0)]
    facts = [Fact(target_id="t", key=f"machine.k{i}", value=i, status=FactStatus.tested, evidence=ev) for i in range(12)]
    answer = Fact(target_id="t", key="owner.answer.which_ai", value="local", status=FactStatus.tested, created_by=CreatedBy.human, evidence=ev)
    ranked = rank_facts(facts + [answer])
    assert ranked[0].key == "owner.answer.which_ai"  # inside any budget of one or more
    assert [f.key for f in rank_facts(facts)] == sorted(f.key for f in facts)  # and without a human fact the order is unchanged

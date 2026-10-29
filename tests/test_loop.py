import asyncio
import json
from pathlib import Path

import pytest

from envrec.adapters import Adapter, BuiltinRing0Adapter, ToolContext
from envrec.contracts import (
    CreatedBy,
    Evidence,
    Fact,
    FactStatus,
    ModelProfile,
    Origin,
    ProbeResult,
    Target,
    TargetKind,
    Tier,
    ToolSpec,
)
from envrec.loop import Loop, RunConfig, build_menu, build_view
from envrec.loop.prompts import SYSTEM_PROMPT
from envrec.model import ScriptedModel, scripted_profile
from envrec.scoring import load_ground_truth, score_target
from envrec.store import Store

ROOT = Path(__file__).resolve().parent.parent
KEYS = ["os.name", "os.version", "os.arch", "os.kernel"]


def spec(name, key=None, *, kinds=(TargetKind.host,), ring=0, tier=Tier.observe, requires=(), tags=()):
    return ToolSpec(name=name, ring=ring, tier=tier, target_kinds=list(kinds), one_line=f"{name} does a thing",
                    source="fake", produces=[key] if key else [], requires=list(requires), tags=list(tags))


class FakeAdapter(Adapter):
    name = "fake"

    def __init__(self, specs, delay=0.0):
        self.specs, self.delay, self.ran = specs, delay, []

    def tool_specs(self):
        return self.specs

    async def run(self, tool, args, ctx):
        if self.delay:
            await asyncio.sleep(self.delay)
        self.ran.append((tool, ctx.target.id))
        s = next(x for x in self.specs if x.name == tool)
        raw = ctx.store.put_raw(f"RAW-SECRET-OUTPUT-{tool}")
        facts = [Fact(target_id=ctx.target.id, key=k, value=f"value-of-{k}", status=FactStatus.tested,
                      evidence=[Evidence(provider="fake", probe=tool, event_id=ctx.event_id, raw_ref=raw,
                                         confidence=0.9)]) for k in s.produces]
        return ProbeResult(ok=True, facts=facts, raw_ref=raw)


def call(tool, **kw):
    return json.dumps({"action": "call_tool", "tool": tool, "why": "next", **kw})


DONE = json.dumps({"action": "done", "why": "finished"})


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "s") as s:
        yield s


@pytest.fixture
def host(store):
    return store.put_target(Target(kind=TargetKind.host, ring=0, handle="this-machine"))


def four_tools():
    return FakeAdapter([spec(f"t{i}", k) for i, k in enumerate(KEYS)])


def run(store, host, replies, adapter=None, profile=None, config=None, **kw):
    adapter = adapter or four_tools()
    model = ScriptedModel(replies, profile or scripted_profile())
    loop = Loop(store, [adapter], model, config=config, **kw)
    summary = asyncio.run(loop.run(host))
    return summary, model, adapter


def events(store, type=None):
    return store.events(type=type)


# ---- a normal run -------------------------------------------------------------------------------

def test_normal_run_records_everything_and_stops_when_nothing_is_left(store, host):
    s, model, adapter = run(store, host, [call("t0"), call("t1"), call("t2"), call("t3")])
    # all four tools answered, nothing unknown: the loop stops by itself, no wasted fifth model call
    assert s.stop_reason == "menu_empty" and s.steps == 4 and len(model.calls) == 4
    assert s.invalid_replies == 0 and s.fallbacks == 0
    assert [t for t, _ in adapter.ran] == ["t0", "t1", "t2", "t3"]
    assert {f.key for f in store.current_facts(host.id)} >= set(KEYS)
    kinds = [e.type for e in events(store)]
    assert kinds[0] == "run_start" and kinds[-1] == "run_end"
    assert {"view_built", "action_chosen", "policy_decision", "tool_run", "tool_result", "ring_closed"} <= set(kinds)
    cov = store.current_fact(host.id, "ring.0.coverage")
    assert cov.value["decision"] == "stop" and s.ring_decisions[host.id] == "stop"


def test_the_model_can_end_the_run_early_with_done(store, host):
    s, model, adapter = run(store, host, [call("t0"), DONE])
    assert s.stop_reason == "done" and s.steps == 2 and len(adapter.ran) == 1
    cov = store.current_fact(host.id, "ring.0.coverage")
    assert cov.value["decision"] == "deepen"  # three tools were never tried: the ring is not finished


def test_tools_that_already_answered_leave_the_menu(store, host):
    run(store, host, [call("t0"), DONE])
    names = [e.name for e in build_menu([four_tools()], host, store, 8)]
    assert "t0" not in names and names == ["t1", "t2", "t3"]


def test_the_menu_is_closed_and_capped(store, host):
    big = FakeAdapter([spec(f"tool{i}", f"hw.cpu_model" if i == 0 else f"hw.k{i}") for i in range(11)])
    assert len(build_menu([big], host, store, 6)) == 6
    assert len(build_menu([big], host, store, 2)) == 2


def test_tools_wait_for_their_prerequisites(store, host):
    adapter = FakeAdapter([spec("late", "host.kind", requires=["os.name"]), spec("early", "os.name")])
    assert [e.name for e in build_menu([adapter], host, store, 8)] == ["early"]
    run(store, host, [call("early"), DONE], adapter=adapter)
    assert [e.name for e in build_menu([adapter], host, store, 8)] == ["late"]


# ---- raw output never reaches a prompt ---------------------------------------------------------------

def test_raw_output_never_enters_any_prompt(store, host):
    _, model, _ = run(store, host, [call("t0"), call("t1"), DONE])
    assert model.calls
    for system, user in model.calls:
        assert "RAW-SECRET" not in system and "RAW-SECRET" not in user
    assert "value-of-os.name" in model.calls[1][1]  # facts do appear, raw does not
    raw_ref = store.current_fact(host.id, "os.name").evidence[0].raw_ref
    assert store.read_raw(raw_ref) == b"RAW-SECRET-OUTPUT-t0"  # it is in the store, hash-checked


def test_expand_fact_shows_evidence_not_raw(store, host):
    expand = json.dumps({"action": "expand_fact", "key": "os.name"})
    _, model, _ = run(store, host, [call("t0"), expand, DONE])
    after = model.calls[2][1]
    assert "fake/t0 conf 0.90" in after and "RAW-SECRET" not in after


# ---- a model that misbehaves ---------------------------------------------------------------------------

def test_invalid_json_twice_falls_back_to_record_unknown_and_the_run_goes_on(store, host):
    s, model, _ = run(store, host, ["not json", "still {not json", call("t0"), DONE])
    assert (s.invalid_replies, s.fallbacks) == (2, 1)
    unknown = store.current_fact(host.id, "host.identity")
    assert unknown.status == FactStatus.unknown and unknown.created_by == CreatedBy.model
    types = [e.type for e in events(store)]
    assert types.count("model_invalid") == 2 and "model_fallback" in types
    assert store.current_fact(host.id, "os.name") is not None  # recovered on the next step


def test_a_tool_not_on_the_menu_is_rejected_and_logged(store, host):
    s, model, adapter = run(store, host, [call("teleport"), call("t0"), DONE])
    assert s.rejected_actions == 1 and adapter.ran == [("t0", host.id)]
    rej = events(store, "action_rejected")[0]
    assert "not on the menu" in rej.payload["reason"]
    assert "rejected" in model.calls[1][1]  # the model is told why on the next turn


def test_a_tool_that_exists_but_is_not_offered_yet_is_also_rejected(store, host):
    adapter = FakeAdapter([spec("late", "host.kind", requires=["os.name"]), spec("early", "os.name")])
    s, _, a = run(store, host, [call("late"), call("early"), DONE], adapter=adapter)
    assert s.rejected_actions == 1 and [t for t, _ in a.ran] == ["early"]


def test_unsupported_actions_are_rejected_not_executed(store, host):
    research = json.dumps({"action": "research", "question": "what is this?"})
    probe = json.dumps({"action": "propose_probe", "code": "print(1)", "test_plan": "x"})
    s, _, _ = run(store, host, [research, probe, DONE])
    assert s.rejected_actions == 2


# ---- policy in the loop -------------------------------------------------------------------------------------

def any_kind_tool(**kw):
    return spec("probe_any", "net.vendor_hint", kinds=(TargetKind.host, TargetKind.net_host), tier=Tier.read, **kw)


def test_unauthorized_target_is_denied_by_policy_and_logged(store, host):
    pi = store.put_target(Target(kind=TargetKind.net_host, ring=2, handle="pi.local"))
    adapter = FakeAdapter([any_kind_tool()])
    s, model, a = run(store, host, [call("probe_any", target_id=pi.id), DONE], adapter=adapter)
    assert s.policy_denied == 1 and a.ran == []
    decision = events(store, "policy_decision")[0].payload
    assert decision["decision"] == "deny" and "authorized" in decision["reason"]
    assert store.current_fact(pi.id, "net.vendor_hint") is None
    assert "denied by policy" in model.calls[1][1]


def test_the_same_call_on_a_config_authorized_target_runs(store, host):
    pi = store.put_target(Target(kind=TargetKind.net_host, ring=2, handle="pi.local", authorized=True),
                          from_config=True)
    adapter = FakeAdapter([any_kind_tool()])
    s, _, a = run(store, host, [call("probe_any", target_id=pi.id), DONE], adapter=adapter)
    assert s.policy_denied == 0 and a.ran == [("probe_any", pi.id)]


def test_a_denied_tool_is_never_offered_on_the_menu(store, host):
    adapter = FakeAdapter([spec("tidy_up", "os.name", tags=["delete"]), spec("fine", "os.arch")])
    assert [e.name for e in build_menu([adapter], host, store, 8)] == ["fine"]


def test_write_with_approval_waits_for_a_human(store):
    box = store.put_target(Target(kind=TargetKind.host, ring=0, handle="lab-box", authorized=True), from_config=True)
    adapter = FakeAdapter([spec("set_thing", "os.name", tier=Tier.write_approval)])
    s, _, a = run(store, box, [call("set_thing"), DONE], adapter=adapter, approve=lambda t, g: False)
    assert a.ran == [] and events(store, "approval")[0].payload["granted"] is False
    s2, _, a2 = run(store, box, [call("set_thing"), DONE], adapter=FakeAdapter(adapter.specs),
                    approve=lambda t, g: True)
    assert [t for t, _ in a2.ran] == ["set_thing"]


# ---- caps -------------------------------------------------------------------------------------------------------

def test_step_cap(store, host):
    s, model, adapter = run(store, host, [call("t0"), call("t1"), call("t2"), call("t3")],
                            config=RunConfig(max_steps=2))
    assert s.stop_reason == "step_cap" and s.steps == 2 and len(adapter.ran) == 2
    assert events(store)[-1].payload["stop_reason"] == "step_cap"


def test_time_cap(store, host):
    slow = FakeAdapter([spec(f"t{i}", k) for i, k in enumerate(KEYS)], delay=0.3)
    s, _, adapter = run(store, host, [call("t0"), call("t1"), call("t2"), call("t3")], adapter=slow,
                        config=RunConfig(max_seconds=0.2))
    assert s.stop_reason == "time_cap" and len(adapter.ran) == 1
    assert store.current_fact(host.id, "ring.0.coverage") is not None  # rings still close on a cap


# ---- the one dial -------------------------------------------------------------------------------------------------

def test_view_budget_changes_the_view_size_and_nothing_else(store, host):
    run(store, host, [call("t0"), call("t1"), call("t2"), call("t3"), DONE])
    small = scripted_profile(view_budget_facts=2, menu_max=6)
    big = scripted_profile(view_budget_facts=20, menu_max=6)
    menu = build_menu([four_tools()], host, store, 6)
    v_small = build_view(store, host, small, menu, "x", {})
    v_big = build_view(store, host, big, menu, "x", {})
    fact_lines = lambda v: [ln for ln in v.splitlines() if ln.startswith("  ") and " = " in ln]  # noqa: E731
    assert len(fact_lines(v_small)) == 2 and len(fact_lines(v_big)) == 4
    strip = lambda v: [ln for ln in v.splitlines() if ln not in fact_lines(v) and not ln.startswith("  (+")]  # noqa: E731
    assert strip(v_small) == strip(v_big)  # headers, menu, actions, last result: identical
    assert "(+2 more facts" in v_small and "more facts" not in v_big
    assert SYSTEM_PROMPT  # the system prompt is a constant: it cannot differ by profile


def test_playbook_cards_go_only_to_profiles_that_list_them(store, host):
    plain = scripted_profile()
    carded = ModelProfile(**{**plain.model_dump(), "playbooks": ["host"]})
    menu = build_menu([four_tools()], host, store, 6)
    pb = ROOT / "playbooks"
    assert "Playbook: host" in build_view(store, host, carded, menu, "x", {}, pb)
    assert "Playbook" not in build_view(store, host, plain, menu, "x", {}, pb)


# ---- the human ------------------------------------------------------------------------------------------------------

def test_ask_human_stores_the_answer_with_created_by_human(store, host):
    ask = json.dumps({"action": "ask_human", "key": "host.kind", "question": "What kind of machine is this?"})
    s, _, _ = run(store, host, [ask, DONE], ask_human=lambda q: "a laptop")
    fact = store.current_fact(host.id, "host.kind")
    assert fact.value == "a laptop" and fact.created_by == CreatedBy.human
    assert fact.status == FactStatus.tested and fact.evidence[0].provider == "human"
    assert events(store, "human_question")[0].payload["question"].startswith("What kind")


def test_ask_human_without_an_answer_leaves_it_unknown(store, host):
    ask = json.dumps({"action": "ask_human", "key": "host.kind", "question": "What is this?"})
    run(store, host, [ask, DONE])
    assert store.current_fact(host.id, "host.kind") is None
    assert events(store, "human_question")


def test_with_tools_exhausted_and_unknowns_open_the_model_still_gets_one_chance_to_ask(store, host):
    store.write_fact(Fact(target_id=host.id, key="host.kind"))  # an open unknown
    ask = json.dumps({"action": "ask_human", "key": "host.kind", "question": "what is it?"})
    s, model, _ = run(store, host, [ask], adapter=FakeAdapter([]), ask_human=lambda q: "a laptop")
    assert len(model.calls) == 1 and "(no tools left)" in model.calls[0][1]
    assert s.stop_reason == "menu_empty"  # asked once, then nothing is left to do


# ---- extend ---------------------------------------------------------------------------------------------------------------

def two_ring_adapter():
    return FakeAdapter([spec("t0", "os.name"), spec("u0", "usb.vendor_id", kinds=(TargetKind.usb,), ring=1)])


def test_fixed_order_profiles_have_the_code_walk_the_rings(store, host):
    usb = store.put_target(Target(kind=TargetKind.usb, ring=1, handle="replay:a", parent=host.id))
    s, model, a = run(store, host, [call("t0"), call("u0")], adapter=two_ring_adapter())
    assert a.ran == [("t0", host.id), ("u0", usb.id)] and len(model.calls) == 2  # the model never chose to move
    assert [e.payload["by"] for e in events(store, "ring_extended")] == ["code"]
    assert s.ring_decisions[host.id] == "extend" and s.ring_decisions[usb.id] == "stop"


def test_model_decides_profiles_choose_when_to_extend(store, host):
    from envrec.contracts import RingPolicy
    usb = store.put_target(Target(kind=TargetKind.usb, ring=1, handle="replay:a", parent=host.id))
    profile = scripted_profile(ring_policy=RingPolicy.model_decides)
    extend = json.dumps({"action": "extend_to", "ring": 1, "why": "ring 0 is done"})
    s, model, a = run(store, host, [call("t0"), extend, call("u0")], adapter=two_ring_adapter(), profile=profile)
    assert a.ran == [("t0", host.id), ("u0", usb.id)] and len(model.calls) == 3  # the extend was the model's call
    assert events(store, "ring_extended")[0].payload["ring"] == 1
    assert set(s.ring_decisions) == {host.id, usb.id}
    assert store.current_fact(host.id, "ring.0.coverage") and store.current_fact(usb.id, "ring.1.coverage")


def test_extend_to_a_ring_with_no_target_is_rejected(store, host):
    s, _, _ = run(store, host, [json.dumps({"action": "extend_to", "ring": 1}), DONE], adapter=two_ring_adapter())
    assert s.rejected_actions == 1


# ---- the real Ring 0 adapter, driven by a scripted model ----------------------------------------------------------------

def test_real_ring0_through_the_loop_scores_against_the_draft_truth(store, host):
    order = ["read_os", "read_os_native", "read_cpu_ram", "read_hw_native", "read_disks", "read_battery",
             "read_display", "read_virtualization", "read_tool_versions", "read_net_interfaces", "classify_host"]
    profile = scripted_profile(view_budget_facts=8, menu_max=12)  # menu wide enough to hold all 11
    s, model, _ = run(store, host, [call(t) for t in order] + [DONE], adapter=BuiltinRing0Adapter(), profile=profile)
    assert s.tools_run == order and s.rejected_actions == 0 and s.policy_denied == 0
    report = score_target(store, host.id, load_ground_truth(ROOT / "ground_truth" / "this-machine.yaml"))
    assert report.count("missing") == 0 and report.provisional
    assert store.current_fact(host.id, "host.kind").status in (FactStatus.hypothesis, FactStatus.unknown)


# ---- repair inside the loop ---------------------------------------------------------------------------------------------

def test_a_tool_name_in_the_action_field_runs_the_tool_and_is_counted_as_repaired(store, host):
    s, model, adapter = run(store, host, [json.dumps({"action": "t0", "why": "first"}), call("t1"), DONE])
    assert s.repaired == 1 and s.invalid_replies == 0 and [t for t, _ in adapter.ran] == ["t0", "t1"]
    assert events(store, "model_repaired")[0].payload["note"].startswith("action 't0'")


def test_with_repair_off_the_same_reply_is_invalid(store, host):
    s, model, adapter = run(store, host, [json.dumps({"action": "t0"})] * 2 + [call("t0"), DONE],
                            config=RunConfig(repair=False))
    assert s.repaired == 0 and s.invalid_replies == 2 and s.fallbacks == 1

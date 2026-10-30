"""S2: protocols are SKILL.md folders whose states the loop enforces. Guidance is markdown; the constraints (menu per step, evidence to advance) are code."""

from __future__ import annotations

import asyncio
import json

import pytest

from envrec.adapters import Adapter
from envrec.contracts import ActionType, Evidence, Fact, FactStatus, ProbeResult, RingPolicy, Target, TargetKind, Tier, ToolSpec
from envrec.contracts.models import Action
from envrec.loop import Loop, RunConfig
from envrec.model import ScriptedModel, scripted_profile
from envrec.protocols import EvidenceView, ProtocolRun, ProtocolSet
from envrec.skills.lib import Library
from envrec.store import Store

LIB = Library().load()
PS = ProtocolSet(LIB.protocols())


def spec(name, key=None, tags=()):
    return ToolSpec(name=name, ring=0, tier=Tier.observe, target_kinds=[TargetKind.host], one_line=f"{name} does a thing", source="fake", produces=[key] if key else [], tags=list(tags))


class Fake(Adapter):
    name = "fake"

    def __init__(self, specs):
        self.specs, self.ran = specs, []

    def tool_specs(self):
        return self.specs

    async def run(self, tool, args, ctx):
        self.ran.append(tool)
        s = next(x for x in self.specs if x.name == tool)
        raw = ctx.store.put_raw(f"raw {tool}")
        facts = [Fact(target_id=ctx.target.id, key=k, value="v", status=FactStatus.tested, evidence=[Evidence(provider="fake", probe=tool, event_id=ctx.event_id, raw_ref=raw, confidence=0.9)])
                 for k in s.produces]
        return ProbeResult(ok=True, facts=facts, raw_ref=raw)


def host_tools():
    return Fake([spec("read_os", "os.name"), spec("read_cpu_ram", "hw.ram_total_gib"), spec("read_disks", "hw.disk_total_gib"), spec("read_battery", "hw.battery_present"),
                 spec("read_display", "hw.display_present"), spec("read_virtualization", "host.virtualization"), spec("read_net_interfaces", "net.interfaces"),
                 spec("classify_host", "host.kind"), spec("unrelated_probe", "x.y", tags=["whatever"])])


def call(tool):
    return json.dumps({"action": "call_tool", "tool": tool, "why": "next"})


def advance(to):
    return json.dumps({"action": "advance", "to": to, "why": "next"})


DONE = json.dumps({"action": "done", "why": "finished"})


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "s") as s:
        yield s


@pytest.fixture
def host(store):
    return store.put_target(Target(kind=TargetKind.host, ring=0, handle="this-machine"))


def drive(store, host, replies, *, policy=RingPolicy.fixed_order, adapter=None, protocols=PS, steps=30):
    profile = scripted_profile().model_copy(update={"ring_policy": policy, "walk_for_model": policy == RingPolicy.fixed_order})
    if protocols is not None and replies and replies[-1] == DONE:
        replies = [*replies, DONE, DONE, DONE]  # a protocol refuses `done` until it has ended (up to three times), so the model asks again
    model = ScriptedModel(replies, profile)
    adapter = adapter or host_tools()
    loop = Loop(store, [adapter], model, config=RunConfig(max_steps=steps, protocols=protocols, library=LIB))
    summary = asyncio.run(loop.run(host))
    return summary, model, adapter, loop


def refusals(store):
    """Refusals of advance and of actions; the refusals of `done` (a protocol that has not ended) are counted apart."""
    return [e for e in store.events(type="protocol_refused") if e.payload.get("action") != "done"]


def last_view(model):
    return model.calls[-1][1] if isinstance(model.calls[-1], tuple) else str(model.calls[-1])


# ---- every kind has a protocol, and they are well formed ------------------------------------------------------------------------------

def test_every_target_kind_resolves_to_a_protocol():
    for kind in TargetKind:
        assert PS.for_kind(kind.value) is not None, kind
    assert PS.for_kind("remote").meta.protocol == "connect" and PS.for_kind("media").meta.protocol == "media"  # a computer reached over a transport; this computer's own paired devices and media apps
    assert PS.for_kind("something-new").meta.protocol == "generic"  # an unknown kind falls to the generic ladder
    wanted = {"host", "usb", "serial", "ble", "network_host", "software", "sensor", "instrument", "signal", "connect", "media", "generic"}
    assert wanted <= set(PS.by_id)


def test_protocol_descriptions_are_triggers_and_bodies_are_guidance():
    for s in LIB.protocols():
        assert s.description.startswith("Use when") and len(s.body.splitlines()) < 500
        assert s.meta.states.states[s.meta.states.start]


def test_a_protocol_with_a_dangling_transition_or_no_end_is_rejected():
    from envrec.skills.meta import ProtocolDef
    with pytest.raises(ValueError, match="not defined"):
        ProtocolDef.model_validate({"kinds": ["x"], "start": "a", "states": {"a": {"advance": [{"to": "nowhere"}]}, "z": {"terminal": True}}})
    with pytest.raises(ValueError, match="terminal"):
        ProtocolDef.model_validate({"kinds": ["x"], "start": "a", "states": {"a": {}}})


# ---- evidence is required to advance --------------------------------------------------------------------------------------------------

def test_a_transition_without_its_evidence_is_refused_with_a_typed_message_and_counted(store, host):
    s, model, adapter, loop = drive(store, host, [advance("context"), advance("context"), DONE])
    assert len(refusals(store)) == 2 and s.stop_reason == "refused_done_loop"  # done is refused while the protocol has steps left
    refused = refusals(store)
    assert len(refused) == 2 and "os.name" in " ".join(refused[0].payload["missing"]) and refused[0].payload["from"] == "basics"
    assert loop._proto_runs[host.id].state == "basics"  # it never moved


def test_a_step_cannot_be_skipped(store, host):
    s, *_ = drive(store, host, [advance("judge"), DONE])
    ev = refusals(store)[0]
    assert len(refusals(store)) == 1 and "no transition from basics to judge" in ev.payload["missing"][0]


def test_with_the_evidence_in_the_model_may_advance(store, host):
    s, model, adapter, loop = drive(store, host, [call("read_os"), call("read_cpu_ram"), call("read_disks"), advance("context"), DONE], policy=RingPolicy.model_decides)
    run = loop._proto_runs[host.id]
    assert len(refusals(store)) == 0 and run.state == "context" and run.history[-1] == {"state": "context", "by": "model"}
    assert len(store.events(type="protocol_state")) == 1


def test_an_unknown_that_was_tried_counts_as_evidence(store, host):
    store.write_fact(Fact(target_id=host.id, key="os.name"))  # recorded unknown: the probe was tried
    store.write_fact(Fact(target_id=host.id, key="hw.ram_total_gib"))
    store.write_fact(Fact(target_id=host.id, key="hw.disk_total_gib"))
    s, *_ , loop = drive(store, host, [advance("context"), DONE], policy=RingPolicy.model_decides)
    assert len(refusals(store)) == 0 and loop._proto_runs[host.id].state == "context"


# ---- the menu is cut to the step ----------------------------------------------------------------------------------------------------------

def test_the_menu_in_each_step_holds_only_what_the_step_allows(store, host):
    s, model, adapter, loop = drive(store, host, [call("read_os"), DONE], policy=RingPolicy.model_decides)
    views = [e.payload["menu"] for e in store.events(type="view_built")]
    assert set(views[0]) == {"read_os", "read_cpu_ram", "read_disks"}  # basics: not the battery, not classify_host, not the unrelated probe
    assert "read_os" not in views[1]


def test_a_tool_outside_the_step_is_not_on_the_menu_so_it_cannot_be_called(store, host):
    s, model, adapter, loop = drive(store, host, [call("read_battery"), DONE], policy=RingPolicy.model_decides)
    assert adapter.ran == [] and s.rejected_actions == 1


def test_actions_a_step_does_not_list_are_refused(store, host):
    research = json.dumps({"action": "research", "question": "what is this?"})
    s, *_ = drive(store, host, [research, DONE], policy=RingPolicy.model_decides)
    assert len(refusals(store)) == 1 and s.rejected_actions == 1
    assert "not allowed in protocol step basics" in [e.payload["reason"] for e in store.events(type="action_rejected")][0]


def test_the_view_carries_the_card_of_the_current_step(store, host):
    drive(store, host, [call("read_os"), DONE], policy=RingPolicy.model_decides)
    view = [e for e in store.events(type="view_built")]
    assert view  # the card itself is in the prompt text the model got
    s, model, *_ = drive(store, host, [DONE], policy=RingPolicy.model_decides)
    text = model.prompts[0] if hasattr(model, "prompts") else str(model.calls[0])
    assert 'PROTOCOL host, you are in the step named "basics" (step 1 of 5)' in text and 'TO MOVE ON: reply {"action":"advance","to":"context"}' in text


# ---- a small model is never stuck, and a protocol ends ------------------------------------------------------------------------------------

def test_for_a_small_model_code_walks_the_steps_when_each_is_finished_and_the_protocol_ends(store, host):
    replies = [call(t) for t in ("read_os", "read_cpu_ram", "read_disks", "read_battery", "read_display", "read_virtualization", "read_net_interfaces", "classify_host")] + [DONE]
    s, model, adapter, loop = drive(store, host, replies)
    run = loop._proto_runs[host.id]
    assert [h["state"] for h in run.history] == ["basics", "context", "judge", "survey", "done"]
    assert [h["by"] for h in run.history] == ["start", "code", "code", "code", "code"]
    assert s.stop_reason == "protocol_done" and adapter.ran == ["read_os", "read_cpu_ram", "read_disks", "read_battery", "read_display", "read_virtualization", "read_net_interfaces", "classify_host"]
    assert len(refusals(store)) == 0 and s.protocol_history[host.id][-1]["state"] == "done"


def test_code_never_writes_a_missing_fact_to_get_a_step_passed(store, host):
    """Nothing reads the disks. The step needs hw.disk_total_gib: code must not record it as unknown to move on; the run ends with a typed stop, incomplete."""
    a = Fake([spec("read_os", "os.name"), spec("read_cpu_ram", "hw.ram_total_gib")])
    s, model, adapter, loop = drive(store, host, [call("read_os"), call("read_cpu_ram"), DONE], adapter=a)
    assert store.current_fact(host.id, "hw.disk_total_gib") is None
    assert loop._proto_runs[host.id].state == "basics" and s.stop_reason == "refused_done_loop"
    assert [e for e in store.events(type="unknown_recorded") if e.payload.get("by") == "code"] == []


def test_a_strong_model_decides_by_itself_and_is_not_moved_by_code(store, host):
    s, model, adapter, loop = drive(store, host, [call("read_os"), call("read_cpu_ram"), call("read_disks"), DONE], policy=RingPolicy.model_decides)
    assert loop._proto_runs[host.id].state == "basics"  # everything in the step is done, but only the model's `advance` moves it


def test_without_protocols_the_loop_is_unchanged(store, host):
    small = Fake([spec("read_battery", "hw.battery_present"), spec("unrelated_probe", "x.y")])
    s, model, adapter, loop = drive(store, host, [call("read_battery"), call("unrelated_probe"), DONE], protocols=None, policy=RingPolicy.model_decides, adapter=small)
    assert adapter.ran == ["read_battery", "unrelated_probe"] and s.protocol_refusals == 0 and loop._proto_runs == {}


def test_the_machine_alone_is_deterministic(store, host):
    run = ProtocolRun(PS.for_kind("host"))
    ev = EvidenceView(facts={"os.name": Fact(target_id="t", key="os.name", value="x", status=FactStatus.hypothesis, evidence=[Evidence(provider="p", probe="p", confidence=0.5)])})
    assert not run.check("context", ev).ok  # a hypothesis is not the tested evidence this step demands... and the other two facts are missing
    assert run.ready(ev) is None
    assert run.check("nowhere", ev).missing[0].startswith("no transition")


def test_done_is_always_refused_while_the_protocol_has_steps_left_and_a_model_that_insists_ends_the_run_as_incomplete(store, host):
    profile = scripted_profile().model_copy(update={"ring_policy": RingPolicy.model_decides})
    model = ScriptedModel([DONE, DONE, DONE, DONE], profile)
    loop = Loop(store, [host_tools()], model, config=RunConfig(max_steps=10, protocols=PS))
    s = asyncio.run(loop.run(host))
    refused = [e for e in store.events(type="protocol_refused") if e.payload.get("action") == "done"]
    assert len(refused) == 3 and s.protocol_refusals == 3 and s.steps == 3 and len(model.calls) == 3  # the third refusal ends the run: a fourth done is never honoured
    assert s.stop_reason == "refused_done_loop" and loop._proto_runs[host.id].state == "basics"
    assert "protocol has not ended" in refused[0].payload["reason"]


def test_asking_the_same_tool_with_the_same_arguments_again_is_refused_and_a_stuck_small_model_is_moved_on(store, host):
    asking = spec("read_os").model_copy(update={"input_model": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}})
    a = Fake([asking, spec("read_cpu_ram", "hw.ram_total_gib")])
    same = json.dumps({"action": "call_tool", "tool": "read_os", "args": {"q": "x"}, "why": "again"})
    s, model, adapter, loop = drive(store, host, [same, same, same, DONE], adapter=a)
    assert adapter.ran == ["read_os"] and s.repeat_refusals >= 1
    assert any(e.type == "repeat_refused" for e in store.events())


def test_a_step_that_closes_on_a_tool_must_have_an_exit_to_close_into():
    from pydantic import ValidationError

    from envrec.skills.meta import ProtocolDef
    ok = {"kinds": ["host"], "start": "a", "states": {"a": {"close_on": ["t"], "advance": [{"to": "b", "needs": [{"tool": "t"}]}]}, "b": {"terminal": True}}}
    assert ProtocolDef.model_validate(ok).states["a"].close_on == ["t"]
    bad = {"kinds": ["host"], "start": "a", "states": {"a": {"close_on": ["t"]}, "b": {"terminal": True}}}
    with pytest.raises(ValidationError, match="closes on"):
        ProtocolDef.model_validate(bad)


def test_the_card_of_a_closing_step_says_so_from_the_spec():
    from envrec.skills.meta import ProtocolDef
    d = ProtocolDef.model_validate({"kinds": ["host"], "start": "a", "states": {"a": {"close_on": ["t"], "advance": [{"to": "b", "needs": [{"fact": "k.x"}]}]}, "b": {"terminal": True}}})
    skill = type("S", (), {"meta": type("M", (), {"states": d, "protocol": "x"})(), "name": "x"})()
    assert "THIS STEP ENDS when t succeeds and the evidence for an exit is in" in ProtocolRun(skill).card()

"""LOOP (L1-L5). Each check is named after the requirement it tests (docs/HARNESS_SPEC.md)."""
import asyncio
import json

import pytest

from envrec.model.parse import ActionParseError, parse_action_repairing
from envrec.model.requester import request_action
from envrec.model.scripted import ScriptedModel
from envrec.contracts import Action, ActionType

from conftest import ROOT, S, TOOL_JSON

NEEDS_A = [("end", [{"tool": "a"}])]
TWO_STEP = {"s1": S("run a", tags=["x"], advance=NEEDS_A), "end": S(terminal=True)}
DONE = '{"action": "done"}'


def test_L1_an_invalid_reply_gets_one_retry_that_carries_the_exact_error_text():
    fallback = Action(action=ActionType.record_unknown, key="host.identity")
    m = ScriptedModel(["this is not json", '{"action": "done"}'])
    res = asyncio.run(request_action(m, "sys", "view", fallback=fallback))
    assert len(m.calls) == 2 and res.used_fallback is False and res.action.action == ActionType.done
    with pytest.raises(ActionParseError) as exc:
        parse_action_repairing("this is not json", None)
    assert str(exc.value) in m.calls[1][1] and "Your previous reply was rejected" in m.calls[1][1]


def test_L1_two_invalid_replies_end_in_a_logged_counted_fallback(run_loop):
    r = run_loop(["nope", "still nope"], {"a": {}}, max_steps=1)
    chosen = r.events("action_chosen")
    assert r.summary.fallbacks == 1 and r.summary.invalid_replies == 2 and chosen[0].payload["fallback"] is True


def test_L1_repair_never_changes_which_tool_was_chosen_and_is_counted(run_loop):
    act, note = parse_action_repairing('{"action": "tool_a"}', {"tool_a", "tool_b"})
    assert (act.action, act.tool) == (ActionType.call_tool, "tool_a") and note
    with pytest.raises(ActionParseError):
        parse_action_repairing('{"action": "tool_c"}', {"tool_a", "tool_b"})  # outside the menu: never repaired into a choice
    r = run_loop(['{"action": "tool_a"}'], {"tool_a": {}, "tool_b": {}}, max_steps=1)
    assert r.summary.repaired == 1 and r.adapter.executed == ["tool_a"]


def test_L2_a_tool_that_is_not_on_the_menu_never_executes(run_loop):
    r = run_loop([TOOL_JSON % "hidden"], {"visible": {}, "hidden": {"kinds": ["usb"]}}, max_steps=1)
    assert r.adapter.executed == [] and [e.payload["reason"] for e in r.events("action_rejected")] == ["'hidden' is not on the menu"]


def test_L2_a_tool_of_another_protocol_state_never_executes(run_loop):
    states = {"s1": S("a", tags=["one"], advance=[("s2", [{"tool": "a"}])]), "s2": S("b", tags=["two"], advance=[("end", [{"tool": "b"}])]), "end": S(terminal=True)}
    r = run_loop([TOOL_JSON % "b"], {"a": {"tags": ["one"]}, "b": {"tags": ["two"]}}, states=states, start="s1", max_steps=1)
    assert r.adapter.executed == [] and r.events("action_rejected")


def test_L3_code_does_not_fabricate_the_evidence_that_moves_a_step_forward(run_loop):
    """A step that needs a fact nothing produced must not be passed by code writing 'unknown' for it and moving on."""
    states = {"s1": S("learn", tags=["none"], advance=[("end", [{"tried": "sensor.reading"}])]), "end": S(terminal=True)}
    r = run_loop([DONE] * 6, {"unrelated": {"tags": ["other"]}}, states=states, start="s1", walk_for_model=True, max_steps=6)
    moves = [h for h in r.summary.protocol_history[r.target.id] if h["by"] == "code"]
    written = [f.key for f in r.store.current_facts(r.target.id)]
    assert moves == [] and "sensor.reading" not in written, (moves, written)


def test_L3_the_walk_for_the_model_dial_is_off_in_the_profiles_the_missions_use():
    import yaml
    profiles = yaml.safe_load((ROOT / "config" / "models.yaml").read_text())["profiles"]
    on = sorted(n for n, p in profiles.items() if p.get("walk_for_model", False) or p.get("ring_policy") == "fixed_order")
    assert on == [], f"profiles with the code-walks-for-the-model dial on: {on}"


def test_L3_every_step_that_code_took_for_the_model_is_tagged_SCRIPT_in_the_log(run_loop):
    r = run_loop([TOOL_JSON % "a"], {"a": {"tags": ["x"]}}, states=TWO_STEP, start="s1", walk_for_model=True, max_steps=3)
    forced = [e for e in r.events("protocol_state") if e.payload.get("by") == "code"]
    assert forced, "the scenario should have produced a code-made advance"
    assert all(e.effective_tag == "SCRIPT" and e.tag == "SCRIPT" for e in forced), [(e.actor.value, e.tag) for e in forced]


def test_L4_done_is_refused_for_as_long_as_the_protocol_has_not_ended(run_loop):
    r = run_loop([DONE] * 8, {"a": {"tags": ["x"]}}, states=TWO_STEP, start="s1", max_steps=8)
    assert r.summary.stop_reason != "done", f"done was accepted after {r.summary.protocol_refusals} refusals with the protocol unfinished"


def test_L4_the_step_cap_stops_the_run(run_loop):
    r = run_loop(['{"action": "record_unknown", "key": "sensor.x"}'] * 10, {"a": {}}, max_steps=3)
    assert r.summary.stop_reason == "step_cap" and r.summary.steps == 3


def test_L4_the_time_cap_stops_the_run(run_loop):
    r = run_loop([DONE] * 3, {"a": {}}, max_seconds=0.0)
    assert r.summary.stop_reason == "time_cap" and r.summary.steps == 0


def test_L4_the_same_action_again_with_nothing_new_is_refused(run_loop):
    same = '{"action": "call_tool", "tool": "a", "args": {"x": 1}}'
    r = run_loop([same, same], {"a": {"needs_arg": True}}, max_steps=2)
    assert r.adapter.executed == ["a"] and r.summary.repeat_refusals == 1


def test_L4_a_repeated_ask_human_is_rejected(run_loop):
    asked = []
    r = run_loop(['{"action": "ask_human", "key": "sensor.x", "question": "what is it?"}'] * 2, {"a": {}}, max_steps=2, ask_human=lambda q: asked.append(q) or "a thermometer")
    assert len(asked) == 1 and len(r.events("action_rejected")) == 1


def test_L4_with_no_human_available_the_run_ends_as_blocked_needs_human(run_loop):
    r = run_loop(['{"action": "ask_human", "key": "sensor.x", "question": "what is it?"}'] * 3, {"a": {}}, max_steps=3, ask_human=lambda q: None)
    assert r.summary.stop_reason == "blocked_needs_human"


L5_REQUIRED = {
    "a digest of the view": lambda p: any(k in p for k in ("view_digest", "view_sha256", "view_hash")),
    "the menu ids": lambda p: "menu" in p,
    "the raw model reply": lambda p: any(k in p for k in ("raw_reply", "reply", "raw")),
    "the validation result": lambda p: any(k in p for k in ("validation", "valid")),
    "the model and profile": lambda p: "model" in p and "profile" in p,
}


@pytest.mark.parametrize("what", sorted(L5_REQUIRED))
def test_L5_every_turn_records(run_loop, what):
    r = run_loop([TOOL_JSON % "a"], {"a": {}}, max_steps=1)
    payloads = [e.payload for e in r.events() if e.type in ("view_built", "action_chosen", "model_call")]
    merged = {k: v for p in payloads for k, v in p.items()}
    assert L5_REQUIRED[what](merged), f"no event of the turn carries {what}; keys seen: {sorted(merged)}"


def test_L5_every_event_carries_one_of_the_four_actor_tags(run_loop):
    """The tag is a field next to the older `actor` (kept for compatibility): MODEL, GUARD, SCRIPT or OWNER."""
    r = run_loop([TOOL_JSON % "a"], {"a": {}}, max_steps=1)
    assert {e.tag for e in r.events()} <= {"MODEL", "GUARD", "SCRIPT", "OWNER"} and None not in {e.tag for e in r.events()}
    assert {e.tag for e in r.events("action_chosen")} == {"MODEL"} and {e.tag for e in r.events("policy_decision")} == {"GUARD"}


def test_L1_a_fallback_is_created_by_the_loop_and_tagged_SCRIPT_never_booked_to_the_model(run_loop):
    from envrec.contracts import CreatedBy
    r = run_loop(["nope", "still nope"], {"a": {}}, max_steps=1)
    facts = [f for f in r.store.current_facts(r.target.id) if f.key == "sensor.identity"]
    assert [f.created_by for f in facts] == [CreatedBy.loop]
    chosen = r.events("action_chosen")
    assert [(e.tag, e.payload["fallback"]) for e in chosen] == [("SCRIPT", True)]
    assert r.events("model_fallback")[0].tag == "SCRIPT"


def test_L5_old_events_without_a_tag_still_load_and_derive_the_same_tag():
    from envrec.contracts import Event
    old = '{"id": "evt_old", "ts": "2026-01-01T00:00:00Z", "actor": "loop", "type": "action_chosen", "payload": {"fallback": true}, "run_id": "r"}'
    e = Event.model_validate_json(old)
    assert e.tag is None and e.effective_tag == "SCRIPT"


def test_L5_the_action_chosen_event_carries_the_raw_reply_the_validation_and_the_menu(run_loop):
    r = run_loop([TOOL_JSON % "a"], {"a": {}, "b": {}}, max_steps=1)
    p = r.events("action_chosen")[0].payload
    assert p["raw_reply"] == TOOL_JSON % "a" and p["validation"] == "valid" and p["menu"] == ["a", "b"]
    assert p["model"] == "scripted" and p["profile"] == "scripted" and len(p["view_digest"]) == 16


def test_L4_a_model_that_keeps_saying_done_ends_as_refused_done_loop_which_is_incomplete(run_loop):
    r = run_loop([DONE] * 9, {"a": {"tags": ["x"]}}, states=TWO_STEP, start="s1", max_steps=9)
    assert r.summary.stop_reason == "refused_done_loop" and r.summary.steps == 3 and r.adapter.executed == []


def test_L4_a_run_whose_turns_add_nothing_ends_as_no_progress(run_loop):
    note = '{"action": "expand_fact", "key": "sensor.nothing"}'
    r = run_loop([note] * 9, {"a": {}}, max_steps=9)
    assert r.summary.stop_reason == "no_progress" and r.summary.steps == 6  # one turn that set the baseline, then five that added nothing


def test_L3_a_protocol_step_is_passed_only_by_a_tool_the_model_chose_that_succeeded(run_loop):
    """A tool that fails (a permission, an error) is not evidence: the step does not move."""
    import asyncio
    from conftest import FakeAdapter
    from envrec.contracts import ProbeResult

    class Failing(FakeAdapter):
        async def run(self, tool, args, ctx):
            self.executed.append(tool)
            return ProbeResult(ok=False, error="permission: denied by the operating system")

    from envrec.loop import Loop, RunConfig
    from envrec.model.scripted import ScriptedModel, scripted_profile
    from envrec.contracts import Target, TargetKind
    from envrec.protocols import ProtocolSet
    from envrec.skills.lib import Library
    from envrec.store import Store
    import tempfile, pathlib
    from conftest import make_protocol
    with tempfile.TemporaryDirectory() as d, Store(pathlib.Path(d) / "s") as st:
        make_protocol(pathlib.Path(d) / "skills", "sensor", TWO_STEP, "s1")
        lib = Library(root=pathlib.Path(d) / "skills").load()
        t = st.put_target(Target(kind=TargetKind.sensor, ring=0, handle="x"))
        ad = Failing({"a": {"tags": ["x"]}})
        model = ScriptedModel([TOOL_JSON % "a", '{"action": "advance", "to": "end"}', DONE, DONE, DONE], scripted_profile(walk_for_model=True))
        s = asyncio.run(Loop(st, [ad], model, config=RunConfig(max_steps=6, protocols=ProtocolSet.from_library(lib), library=lib)).run(t))
        assert ad.executed == ["a"] and st.tools_run(t.id) == set()
        assert s.protocol_history[t.id][-1]["state"] == "s1" and s.stop_reason != "protocol_done"


def test_L3_a_run_with_the_dial_on_says_so_and_a_run_without_it_does_not(run_loop):
    on = run_loop([TOOL_JSON % "a"], {"a": {}}, max_steps=1, walk_for_model=True)
    off = run_loop([TOOL_JSON % "a"], {"a": {}}, max_steps=1)
    assert on.summary.walk_for_model is True and on.events("run_start")[0].payload["walk_for_model"] is True
    assert off.summary.walk_for_model is False and off.events("run_start")[0].payload["walk_for_model"] is False


def test_L2_the_model_chooses_the_target_among_the_candidates_it_is_shown(run_loop, tmp_path):
    import asyncio
    from envrec.contracts import Target, TargetKind
    from envrec.loop import Loop, RunConfig
    from envrec.model.scripted import ScriptedModel, scripted_profile
    from envrec.protocols import ProtocolSet
    from envrec.skills.lib import Library
    from envrec.store import Store
    from conftest import FakeAdapter, make_protocol
    skills = tmp_path / "skills"
    make_protocol(skills, "scope", {"choose": S("pick one", advance=[("end", [{"action": "extend_to"}])]), "end": S(terminal=True)}, "choose")
    lib = Library(root=skills).load()
    with Store(tmp_path / "s") as st:
        root = st.put_target(Target(kind=TargetKind.scope, ring=0, handle="candidates"))
        a = st.put_target(Target(kind=TargetKind.ble, ring=1, handle="distractor-a", parent=root.id))
        b = st.put_target(Target(kind=TargetKind.sensor, ring=1, handle="the-one", parent=root.id))
        ad = FakeAdapter({"probe": {"kinds": ["sensor"]}})
        model = ScriptedModel(['{"action": "extend_to", "target_id": "%s"}' % b.id, TOOL_JSON % "probe"], scripted_profile())
        s = asyncio.run(Loop(st, [ad], model, config=RunConfig(max_steps=3, protocols=ProtocolSet.from_library(lib), library=lib)).run(root))
        first_view = model.calls[0][1]
        assert f"{a.id} kind=ble" in first_view and f"{b.id} kind=sensor" in first_view and "extend_to(target_id)" in first_view
        assert [e.payload["by"] for e in st.events(type="ring_extended")] == ["model"] and ad.executed == ["probe"]
        wrong = ScriptedModel(['{"action": "extend_to", "target_id": "tgt_nonexistent"}'], scripted_profile())
        asyncio.run(Loop(st, [ad], wrong, config=RunConfig(max_steps=1, protocols=ProtocolSet.from_library(lib), library=lib)).run(root))
        assert "is not one of the candidates" in [e.payload["reason"] for e in st.events(type="action_rejected")][-1]


# ---- an empty menu is never silent ------------------------------------------------------------------------------------------------------------------
# The real Bluetooth run (runs/nearby-1793028536) spent four turns with an empty menu: the model ran the tools of a later step while it was still in an earlier one, so each step
# had nothing left to offer, and it had to guess the reply that moves on. Now every such turn is a typed event with its cause, the way forward is printed as the exact reply, and a
# step with nothing to run, no way forward and nobody left to ask ends the run as `menu_empty`.
OUT_OF_ORDER = {"s1": S("first", tags=["x"], advance=[("s2", [{"tool": "a"}])]), "s2": S("second", tags=["x"], advance=[("end", [{"tool": "b"}])]), "end": S(terminal=True)}


def _advance(to):
    return json.dumps({"action": "advance", "to": to})


def test_L4_an_empty_menu_with_the_evidence_in_is_typed_shows_the_exact_way_forward_and_the_model_still_moves(run_loop):
    r = run_loop([TOOL_JSON % "a", TOOL_JSON % "b", _advance("s2"), _advance("end"), DONE], {"a": {"tags": ["x"]}, "b": {"tags": ["x"]}}, states=OUT_OF_ORDER, start="s1", max_steps=6)
    empties = r.events("menu_empty")
    assert [e.payload["cause"] for e in empties] == ["way_forward", "way_forward"] and [e.payload["forward"] for e in empties] == [["s2"], ["end"]]
    assert r.summary.empty_menu_turns == 2 and r.summary.empty_menu_causes == {"way_forward": 2}
    views = [user for _system, user in r.model.calls]
    assert 'WAY FORWARD (this step\'s evidence is in): {"action":"advance","to":"s2"}' in views[2] and '{"action":"advance","to":"end"}' in views[3]
    assert [h["by"] for h in r.summary.protocol_history[r.target.id]] == ["start", "model", "model"]  # code showed the way; the model took it
    assert r.summary.stop_reason == "protocol_done"


def test_L4_every_turn_the_model_was_asked_with_an_empty_menu_has_a_menu_empty_event(run_loop):
    r = run_loop([TOOL_JSON % "a", TOOL_JSON % "b", _advance("s2"), _advance("end"), DONE], {"a": {"tags": ["x"]}, "b": {"tags": ["x"]}}, states=OUT_OF_ORDER, start="s1", max_steps=6)
    built_empty = [e for e in r.events("view_built") if not e.payload["menu"]]
    assert len(built_empty) == len(r.events("menu_empty")) == r.summary.empty_menu_turns


def test_L4_a_step_with_nothing_to_run_no_way_forward_and_nobody_left_to_ask_ends_as_menu_empty_instead_of_dead_turns(run_loop):
    stuck = {"s1": S("first", tags=["x"], advance=[("end", [{"tool": "never_offered"}])]), "end": S(terminal=True)}
    ask = '{"action": "ask_human", "key": "sensor.x", "question": "what now?"}'
    r = run_loop([TOOL_JSON % "a", ask, ask, ask, ask], {"a": {"tags": ["x"]}}, states=stuck, start="s1", max_steps=8, ask_human=lambda q: "no idea")
    assert r.summary.stop_reason == "menu_empty"
    assert r.summary.model_calls == 2  # the tool, then the one question the model was still able to ask; the third turn would have been dead and was never asked
    assert [e.payload["cause"] for e in r.events("menu_empty")] == ["actions", "stuck"]


def test_L4_the_time_cap_does_not_count_the_time_the_owner_took_to_answer(tmp_path):
    """A mission has many approvals and the owner thinks for as long as they need: the cap bounds the run, not the owner."""
    import time

    from conftest import FakeAdapter
    from envrec.contracts import Target, TargetKind
    from envrec.loop import Loop, RunConfig
    from envrec.model.scripted import scripted_profile
    from envrec.store import Store

    class SlowOwner(FakeAdapter):
        owner_wait_s = 0.0

        async def run(self, tool, args, ctx):
            time.sleep(0.4)  # the owner is deciding
            self.owner_wait_s += 0.4
            return await super().run(tool, args, ctx)

    with Store(tmp_path / "s") as st:
        t = st.put_target(Target(kind=TargetKind.sensor, ring=0, handle="x"))
        adapter = SlowOwner({"a": {}, "b": {}})
        loop = Loop(st, [adapter], ScriptedModel([TOOL_JSON % "a", TOOL_JSON % "b", DONE], scripted_profile()), config=RunConfig(max_steps=5, max_seconds=0.5))
        summary = asyncio.run(loop.run(t))
    assert adapter.executed == ["a", "b"] and summary.stop_reason == "menu_empty" and summary.owner_wait_s >= 0.8 and summary.elapsed_s < 0.5


# ---- hang safety: a call that does not answer ends as a typed HANG, never a silent wait ----------------------------------------------------------------

def test_L4_a_model_that_does_not_answer_ends_the_run_as_a_typed_hang(tmp_path, monkeypatch):
    import time as _time

    from envrec.contracts import Target, TargetKind
    from envrec.loop import Loop, RunConfig, run as loop_run
    from envrec.model.base import Completion
    from envrec.model.scripted import scripted_profile
    from envrec.store import Store
    from conftest import FakeAdapter

    class Silent(ScriptedModel):
        async def complete(self, system, user):
            await asyncio.sleep(30)
            return Completion(text=DONE)

    monkeypatch.setattr(loop_run, "MODEL_CALL_TIMEOUT_S", 0.3)
    with Store(tmp_path / "s") as st:
        t = st.put_target(Target(kind=TargetKind.sensor, ring=0, handle="x"))
        t0 = _time.monotonic()
        loop = Loop(st, [FakeAdapter({"a": {}})], Silent([], scripted_profile()), config=RunConfig(max_steps=3, max_seconds=60))
        summary = asyncio.run(loop.run(t))
        assert summary.stop_reason == "HANG" and summary.hangs == 1 and _time.monotonic() - t0 < 5
        assert [e.payload["kind"] for e in st.events(type="hang")] == ["model"]


def test_L4_a_tool_that_does_not_answer_is_given_up_on_as_a_typed_hang_and_the_run_goes_on(tmp_path, monkeypatch):
    import time as _time

    from envrec.contracts import Target, TargetKind
    from envrec.loop import Loop, RunConfig
    from envrec.model.scripted import scripted_profile
    from envrec.store import Store
    from envrec.tools.bridge import RegistryAdapter
    from envrec.tools.registry import REGISTRY, tool
    from envrec.tools.ctx import ToolContext, ToolResult

    @tool(kinds=("sensor",), timeout_s=0.3)
    def aa_stuck_probe(ctx: ToolContext) -> ToolResult:
        """Wait far too long, for the hang test.

        Args:
            none
        Returns:
            Nothing.
        Tier: observe
        Platforms: any
        Notes: test only.
        """
        _time.sleep(1.5)
        return ToolResult(ok=True)

    try:
        with Store(tmp_path / "s") as st:
            t = st.put_target(Target(kind=TargetKind.sensor, ring=0, handle="x"))
            t0 = _time.monotonic()
            loop = Loop(st, [RegistryAdapter(platform="linux")], ScriptedModel([TOOL_JSON % "aa_stuck_probe", DONE], scripted_profile()), config=RunConfig(max_steps=3))
            summary = asyncio.run(loop.run(t))
            assert summary.hangs == 1 and summary.stop_reason == "done"
            assert [e.payload["kind"] for e in st.events(type="hang")] == ["tool"]
            assert "HANG: aa_stuck_probe" in [e.payload["error"] for e in st.events(type="tool_result")][0]
    finally:
        REGISTRY.pop("aa_stuck_probe", None)

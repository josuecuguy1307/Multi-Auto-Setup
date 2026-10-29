"""The loop's honesty contracts: a failed tool is not evidence, an approval asked on a worker thread is still on the record, a first-run skill is SCRIPT, the dial is named."""
import asyncio
import threading

from envrec.contracts import Actor, Event, Fact, FactStatus, ProbeResult, Target, TargetKind
from envrec.contracts.models import Evidence
from envrec.discovery import Salts
from envrec.discovery.types import this_platform
from envrec.store import Store
from envrec.tools import ToolContext, get_tool, run_tool
from envrec.tools.ctx import flush_approvals


def test_a_tool_counts_as_run_only_when_its_result_was_ok(tmp_path):
    with Store(tmp_path / "s") as st:
        t = st.put_target(Target(kind=TargetKind.sensor, ring=0, handle="x"))
        st.append_event(Event(actor=Actor.loop, type="tool_run", payload={"tool": "a", "target": t.id}))
        st.append_event(Event(actor=Actor.loop, type="tool_result", payload={"tool": "a", "target": t.id, "ok": False, "error": "permission"}))
        assert st.tools_run(t.id) == set()  # attempted, failed: not evidence
        st.append_event(Event(actor=Actor.loop, type="tool_run", payload={"tool": "b", "target": t.id}))
        st.append_event(Event(actor=Actor.loop, type="tool_result", payload={"tool": "b", "target": t.id, "ok": True, "facts": []}))
        st.append_event(Event(actor=Actor.loop, type="tool_result", payload={"tool": "c", "target": "another-target", "ok": True, "facts": []}))
        assert st.tools_run(t.id) == {"b"}


def test_an_approval_asked_on_a_worker_thread_is_written_by_the_owner_thread(tmp_path):
    with Store(tmp_path / "s") as st:
        t = st.put_target(Target(kind=TargetKind.serial, ring=0, handle="/dev/ttys998"))
        ctx = ToolContext(None, this_platform(), st, t, Salts(), {}, lambda plan: False)
        worker = threading.Thread(target=lambda: run_tool(get_tool("serial_identify"), ctx, {"port": "/dev/ttys998"}))
        worker.start()
        worker.join()
        assert st.events(type="approval") == []  # the worker must not touch the store (it belongs to the owner thread)
        assert [(e["tool"], e["granted"]) for e in ctx.approvals_log] == [("serial_identify", False)]
        flush_approvals(ctx)
        assert [(e.payload["tool"], e.payload["granted"], e.tag) for e in st.events(type="approval")] == [("serial_identify", False, "OWNER")] and ctx.approvals_log == []


def test_the_skills_the_loop_runs_by_itself_on_a_first_run_are_tagged_SCRIPT_and_are_not_counted_as_the_models_tool_runs():
    from envrec.contracts.tags import derive_tag
    assert derive_tag("loop", "first_rung_identify", {"skill": "serial-banner"}) == "SCRIPT" and derive_tag("loop", "skill_reuse", {}) == "SCRIPT"
    assert derive_tag("loop", "action_chosen", {"fallback": False}) == "MODEL" and derive_tag("loop", "action_chosen", {"fallback": True}) == "SCRIPT"
    assert derive_tag("loop", "protocol_state", {"by": "model"}) == "MODEL" and derive_tag("loop", "protocol_state", {"by": "code"}) == "SCRIPT"
    assert derive_tag("loop", "policy_decision", {}) == "GUARD" and derive_tag("human", "approval", {}) == "OWNER"


def test_the_shipped_profiles_have_the_walk_for_the_model_dial_off():
    from envrec.model import load_profile
    from envrec.cli import DEFAULT_MODELS
    import yaml
    names = sorted(yaml.safe_load(DEFAULT_MODELS.read_text())["profiles"])
    assert names == ["muse-glimmer", "muse-spark", "subscription-cli"]
    for name in names:
        profile, _ = load_profile(DEFAULT_MODELS, name, include_local=False)
        assert profile.walk_for_model is False, name

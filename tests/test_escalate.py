import asyncio
import json

import pytest

from envrec.adapters import Adapter
from envrec.contracts import Fact, FactStatus, Evidence, ProbeResult, Target, TargetKind, Tier, ToolSpec
from envrec.loop import Loop, RunConfig
from envrec.loop.escalate import Escalator
from envrec.model import ScriptedModel, scripted_profile
from envrec.store import Store

CALL = json.dumps({"action": "call_tool", "tool": "t0", "why": "go"})
DONE = json.dumps({"action": "done"})
ASK = json.dumps({"action": "ask_human", "key": "host.kind", "question": "what is this?"})


class OneTool(Adapter):
    name = "one"

    def tool_specs(self):
        return [ToolSpec(name="t0", ring=0, tier=Tier.read, target_kinds=[TargetKind.host], one_line="read thing",
                         source="one", produces=["os.name"])]

    async def run(self, tool, args, ctx):
        return ProbeResult(ok=True, facts=[Fact(target_id=ctx.target.id, key="os.name", value="x", status=FactStatus.tested,
                                                evidence=[Evidence(provider="p", probe="t0", confidence=0.9)])])


class Traced(ScriptedModel):
    def __init__(self, replies, order, label):
        super().__init__(replies, scripted_profile(name=label))
        self.order, self.label = order, label

    async def complete(self, system, user):
        self.order.append(self.label)
        return await super().complete(system, user)


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "s") as s:
        yield s


def run(store, weak_replies, strong_replies, threshold, ask=None):
    order = []
    weak, strong = Traced(weak_replies, order, "weak"), Traced(strong_replies, order, "strong")
    opened = []
    esc = Escalator(strong, threshold, open_tunnel=lambda: opened.append("open"), close_tunnel=lambda: opened.append("close"))
    host = store.put_target(Target(kind=TargetKind.host, ring=0, handle="weak-device"))
    summary = asyncio.run(Loop(store, [OneTool()], weak, config=RunConfig(max_steps=8), escalator=esc, ask_human=ask).run(host))
    return summary, order, esc, opened


@pytest.mark.parametrize("n", [1, 2, 3, 5])
def test_escalation_happens_after_exactly_n_consecutive_invalid_replies(store, n):
    _, order, esc, opened = run(store, ["junk"] * 40, [CALL, DONE, DONE], n)
    first_strong = order.index("strong")
    assert order[:first_strong] == ["weak"] * n  # not before the Nth invalid reply
    assert order[first_strong] == "strong"  # and immediately after it, not later
    assert esc.escalated[0]["consecutive_invalid"] == n and esc.escalated[0]["reason"] == "invalid_threshold"
    assert opened[0] == "open"


def test_a_valid_reply_resets_the_count_so_a_mostly_working_weak_model_never_escalates(store):
    weak = ["junk", CALL, "junk", DONE]
    summary, order, esc, opened = run(store, weak, [CALL], 2)
    assert "strong" not in order and esc.escalated == [] and opened == []


def test_the_tunnel_opens_once_on_first_use_and_closes_at_the_end(store):
    _, _, esc, opened = run(store, ["junk"] * 40, [CALL, DONE, CALL, DONE, DONE], 2)
    assert opened == ["open", "close"]


def test_escalated_turns_are_recorded_with_turn_number_and_reason(store):
    summary, _, _, _ = run(store, ["junk"] * 40, [CALL, DONE], 2)
    ev = store.events(type="escalated")
    assert ev and ev[0].payload["reason"] == "invalid_threshold" and ev[0].payload["turn"] >= 1
    assert summary.escalated_turns == [e.payload for e in ev]


def test_rung_6_on_a_weak_device_is_routed_to_the_controllers_model_first(store):
    asked = []
    summary, order, esc, _ = run(store, [ASK], [CALL, DONE], 99, ask=lambda q: asked.append(q) or "a laptop")
    assert esc.escalated[0]["reason"] == "rung6_ask_human" and "strong" in order
    assert asked == []  # the stronger model resolved the turn, so the human was not bothered
    assert store.current_fact(store.list_targets()[0].id, "os.name").value == "x"


def test_if_the_stronger_model_also_asks_the_human_the_human_is_asked(store):
    asked = []
    run(store, [ASK], [ASK, DONE], 99, ask=lambda q: asked.append(q) or "a laptop")
    assert len(asked) == 1


def test_no_escalator_means_no_change_in_behavior(store):
    host = store.put_target(Target(kind=TargetKind.host, ring=0, handle="h"))
    s = asyncio.run(Loop(store, [OneTool()], ScriptedModel([CALL, DONE]), config=RunConfig(max_steps=4)).run(host))
    assert s.escalated_turns == [] and s.stop_reason in ("done", "menu_empty")

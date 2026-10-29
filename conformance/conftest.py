"""Shared fakes for the conformance checks. Nothing here touches a real device or the network."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from envrec.adapters.base import Adapter  # noqa: E402
from envrec.contracts import ProbeResult, Target, TargetKind, Tier, ToolSpec  # noqa: E402
from envrec.contracts.models import Evidence, Fact  # noqa: E402
from envrec.contracts import FactStatus  # noqa: E402
from envrec.loop import Loop, RunConfig  # noqa: E402
from envrec.model.scripted import ScriptedModel, scripted_profile  # noqa: E402
from envrec.protocols import ProtocolSet  # noqa: E402
from envrec.skills.lib import Library  # noqa: E402
from envrec.store import Store  # noqa: E402

TOOL_JSON = '{"action": "call_tool", "tool": "%s"}'


class FakeAdapter(Adapter):
    """Tools that record every execution. `tools`: name -> {"tags": [...], "kinds": [...], "key": fact key it writes}."""

    name = "fake"

    def __init__(self, tools: dict[str, dict]) -> None:
        self.tools, self.executed = tools, []

    def tool_specs(self) -> list[ToolSpec]:
        return [ToolSpec(name=n, ring=0, tier=Tier.observe, target_kinds=[TargetKind(k) for k in t.get("kinds", ["sensor"])], one_line=f"fake tool {n}", source="fake",
                         tags=list(t.get("tags", [])), produces=[t["key"]] if t.get("key") else [],
                         input_model={"required": ["x"]} if t.get("needs_arg") else {}) for n, t in self.tools.items()]

    async def run(self, tool, args, ctx):
        self.executed.append(tool)
        key = self.tools[tool].get("key") or f"sensor.{tool}"
        ev = Evidence(provider="fake", probe=tool, confidence=0.9)
        return ProbeResult(ok=True, facts=[Fact(target_id=ctx.target.id, key=key, value=1, status=FactStatus.tested, evidence=[ev])])


def make_protocol(root: Path, kind: str, states: dict, start: str) -> None:
    d = root / f"protocol-{kind}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: protocol-{kind}\ndescription: Use when a conformance check needs a throwaway protocol.\n---\n\n# protocol\n\nA test protocol.\n")
    (d / "skill.meta.json").write_text(json.dumps({"provenance": "builtin", "protocol": kind, "tier": "observe", "ring": 0, "platforms": ["any"], "signature": {},
                                                  "states": {"kinds": [kind], "start": start, "states": states}}))


def S(goal="", tags=(), tools=(), advance=(), terminal=False):
    return {"goal": goal, "tags": list(tags), "tools": list(tools), "actions": [], "skills": [], "advance": [{"to": to, "needs": list(needs)} for to, needs in advance],
            "terminal": terminal, "reason": "end" if terminal else ""}


class Run:
    def __init__(self, summary, store, model, adapter, target):
        self.summary, self.store, self.model, self.adapter, self.target = summary, store, model, adapter, target

    def events(self, type=None):
        return self.store.events(type=type) if type else self.store.events()


@pytest.fixture
def run_loop(tmp_path):
    stores = []

    def go(replies, tools, *, states=None, start=None, walk_for_model=False, max_steps=8, max_seconds=60.0, ask_human=None, kind="sensor", repair=True):
        store = Store(tmp_path / f"s{len(stores)}")
        stores.append(store)
        target = store.put_target(Target(kind=TargetKind(kind), ring=0, handle="conformance-fake"))
        lib = protocols = None
        if states:
            make_protocol(tmp_path / f"skills{len(stores)}", kind, states, start)
            lib = Library(root=tmp_path / f"skills{len(stores)}").load()
            protocols = ProtocolSet.from_library(lib)
        model = ScriptedModel(list(replies), scripted_profile(walk_for_model=walk_for_model))
        adapter = FakeAdapter(tools)
        loop = Loop(store, [adapter], model, config=RunConfig(max_steps=max_steps, max_seconds=max_seconds, protocols=protocols, library=lib, repair=repair), ask_human=ask_human)
        summary = asyncio.run(loop.run(target))
        return Run(summary, store, model, adapter, target)

    yield go
    for s in stores:
        s.close()

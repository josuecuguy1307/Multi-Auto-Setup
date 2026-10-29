"""RegistryAdapter: how the loop reaches the tool registry (and, later, the skill library). The loop still talks to Adapters; this one is built from the registry,
so the menu is generated from signatures and docstrings and every result passes the privacy gate on its way into the store."""

from __future__ import annotations

import asyncio
from typing import Any

from envrec.adapters.base import Adapter, ToolContext as LoopContext
from envrec.contracts import Coverage, ProbeResult, ToolSpec, Unknown
from envrec.contracts.models import Fact
from envrec.discovery.sanitize import Salts
from envrec.discovery.types import Entity, FactOut, Reason, UnknownOut, this_platform
from envrec.privacy.gate import PrivacyGate
from envrec.tools.ctx import ToolContext, ToolResult, run_tool
from envrec.tools.registry import Tool, get_tool, tool_specs

_CONFIDENCE = {"rules": 0.7, "native": 0.95, "platform": 0.95, "psutil": 0.9, "stdlib": 0.95, "which": 0.95}
HYPOTHESIS_KEYS = {"host.kind"}  # an inference, not an observation


def result_to_outputs(res: ToolResult) -> list:
    """The typed outputs of a tool that returns fact keys; a value of None becomes an unknown with its reason."""
    outs: list = []
    conf = _CONFIDENCE.get(res.provider or "", 0.8)
    for key, value in (res.facts or {}).items():
        if value is None:
            outs.append(UnknownOut(key, Reason.unavailable, res.reasons.get(key, "could not be determined")))
        else:
            outs.append(FactOut(key, value, conf, list(res.limitations), None, key in HYPOTHESIS_KEYS))
    return outs


class RegistryAdapter(Adapter):
    """The loop's way to the tools AND to the skill library. Tools are the menu as the registry describes them; skills are menu entries with a name and a description. Choosing a
    skill runs its plan (registered tools, then the sandboxed script) and its output goes through the privacy gate like everything else. A skill the model wrote runs on a real
    device only if the human approved that exact script (pinned by its hash) and is re-verified on each new device before its facts count as verified."""

    name = "tools"

    def __init__(self, *, platform: str | None = None, collector=None, salts: Salts | None = None, params: dict | None = None, approve=None, extra_specs=None,
                 library=None, shelf=None, ask_skill_approval=None) -> None:
        self.platform = platform or this_platform()
        self._collector, self.salts, self.params, self.approve = collector, salts or Salts(), params or {}, approve
        self.extra_specs = extra_specs
        self.library, self.shelf = library, shelf
        self.ask_skill_approval = ask_skill_approval  # the human's yes for running a generated skill on a real device; asked once per exact script
        self.skill_runs: list[dict] = []  # every skill run: name, model_calls (always 0 here), facts

    @property
    def collector(self):
        if self._collector is None:
            from envrec.discovery import LocalCollector
            self._collector = LocalCollector(self.platform)
        return self._collector

    def tool_specs(self) -> list[ToolSpec]:
        specs = tool_specs(self.platform)
        if self.library is not None:
            specs += self.library.specs(self.platform)
        if self.extra_specs:
            specs += list(self.extra_specs())
        return specs

    # ---- skills ----------------------------------------------------------------------------------------------------------------------------

    def _skill_ctx(self, ctx: LoopContext) -> ToolContext:
        tctx = ToolContext(self.collector, self.platform, ctx.store, ctx.target, self.salts, dict(self.params), self.approve, ctx.event_id)
        tctx.known_snapshot = tctx.known()
        return tctx

    def run_skill_now(self, skill, ctx: LoopContext, args: dict | None = None) -> ProbeResult:
        """Run one skill on the calling thread (the store is bound to it). Returns what the loop records; nothing is written here."""
        from envrec.skills.runner import run_skill
        from envrec.skills.verify import Approvals, approval_text
        if skill.meta.plan is None:  # guidance only: choosing it brings its body into the view, there is nothing to run
            return ProbeResult(ok=True)
        real = getattr(self.collector, "origin", "local") != "replay" and not ctx.target.handle.startswith("replay:")
        generated = skill.meta.provenance == "generated"
        if generated and real:
            approvals = Approvals(self.library.root)
            if not approvals.approved(skill):
                if self.ask_skill_approval is None or not approvals.approve(skill, approval_text(skill), self.ask_skill_approval):
                    return ProbeResult(ok=False, error=f"the skill {skill.name} was written by a model and the human has not approved this exact script; it did not run")
        tctx = self._skill_ctx(ctx)
        run = run_skill(skill, tctx, params={**(args or {}), **{k: v for k, v in self.params.items() if isinstance(v, (str, int, float))}}, write=False, verified_here=True)
        col = getattr(run, "collected", None)
        if col is not None and self.shelf is not None:
            for c in col.caps.values():
                if c.ok:
                    self.shelf.put(ctx.target.id, c.name, c.text)
        self.skill_runs.append({"skill": skill.name, "status": run.status, "facts": run.facts, "entities": run.entities, "model_calls": 0})
        if run.status in ("denied", "error", "unavailable", "permission", "timeout") and not run.built:
            return ProbeResult(ok=False, error=f"{skill.name}: {run.status}: {run.reason}"[:300])
        built = list(run.built)
        if generated and real:
            built = self._reverify(skill, built, ctx)
        unknowns = [Unknown(key=k, question=v[:200]) for k, v in run.unknown_reasons.items()]
        checked = [f.key for f in built if f.status.value != "unknown"]
        return ProbeResult(ok=True, facts=built, unknowns=unknowns, coverage=Coverage(scope=f"skill.{skill.key}", status="complete" if not unknowns else "partial", checked=checked,
                                                                                    unknown=[u.key for u in unknowns]))

    def _reverify(self, skill, built, ctx: LoopContext):
        """A skill written for one device counts on another only after it is re-verified THERE: its own recorded tests pass again in the sandbox and it produced the same fact
        keys here. Then its facts stand as verified (two providers agree: the skill and its stored tests) and the device is recorded. Otherwise nothing it said counts as more than
        a hypothesis, and says why."""
        import json

        from envrec.contracts import CreatedBy, Evidence, FactStatus
        from envrec.skills.casetest import run_cases
        expected = {k for c in (skill.path / "tests").glob("*/expect.json") for k in json.loads(c.read_text()).get("facts", {})}
        got = {f.key for f in built if f.status != FactStatus.unknown and ".entity." not in f.key}
        ok = bool(expected and expected <= got and all(r.passed for r in run_cases(skill)))
        if not ok:
            lim = "unverified_here: the skill's tests or its fact keys did not hold on this device"
            return [f.model_copy(update={"status": FactStatus.hypothesis, "evidence": [e.model_copy(update={"limitations": [*e.limitations, lim], "confidence": min(e.confidence, 0.5)}) for e in f.evidence]})
                    if f.status != FactStatus.unknown else f for f in built]
        self.library.record_verified(skill.name, ctx.target.id)
        ev = Evidence(provider="skill-reverify", probe="stored tests on this device", event_id=ctx.event_id, confidence=0.9,
                      limitations=["the skill's own tests pass and it produced the same fact keys here"])
        return [f.model_copy(update={"status": FactStatus.verified, "created_by": CreatedBy.verifier, "evidence": [*f.evidence, ev]}) if f.key in expected else f for f in built]

    async def run(self, tool: str, args: dict[str, Any], ctx: LoopContext) -> ProbeResult:
        if tool.startswith("skill.") and self.library is not None:
            sk = self.library.get(tool[6:])
            if sk is None:
                return ProbeResult(ok=False, error=f"no skill named {tool[6:]!r}")
            return self.run_skill_now(sk, ctx, args)
        t: Tool | None = get_tool(tool)
        if t is None:
            return ProbeResult(ok=False, error=f"no tool named {tool!r}")
        tctx = ToolContext(self.collector, self.platform, ctx.store, ctx.target, self.salts, dict(self.params), self.approve, ctx.event_id)
        tctx.known_snapshot = tctx.known()
        res: ToolResult = await asyncio.to_thread(run_tool, t, tctx, args)
        if not res.ok:
            return ProbeResult(ok=False, error=f"{res.error.value if res.error else 'failed'}: {res.detail}"[:300])
        if self.shelf is not None:
            for c in res.captures:
                if c.ok:
                    self.shelf.put(ctx.target.id, c.name, c.text)
        gate = PrivacyGate(ctx.store, ctx.target, self.salts, res.provider or t.name, event_id=ctx.event_id, reducers={c.name: t.reducer_for(c.name) for c in res.captures})
        gate.store_captures(res.captures)
        outs = result_to_outputs(res)
        facts, summary = gate.build(outs)  # the loop writes them, through the gate (see Loop._apply)
        unknowns = [Unknown(key=k, question=v[:200]) for k, v in summary.unknown_reasons.items()]
        raw_ref = next(iter(gate.refs.values()), None)
        checked = [f.key for f in facts if f.status.value != "unknown"]
        return ProbeResult(ok=True, facts=facts, raw_ref=raw_ref, unknowns=unknowns,
                           coverage=Coverage(scope=tool, status="complete" if not unknowns else "partial", checked=checked, unknown=[u.key for u in unknowns]))

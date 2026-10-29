"""The loop (file 06, section A): view -> menu -> one Action -> validate -> policy -> run -> record -> stop check.

Plain asyncio, no agent framework. The model only picks from a menu and fills a form; code
runs everything. Raw output goes to the store and never into a prompt.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from envrec.adapters import Adapter, ToolContext
from envrec.contracts import (
    KIND_PREFIX,
    Action,
    ActionType,
    Actor,
    CreatedBy,
    Decision,
    Event,
    Evidence,
    Fact,
    FactStatus,
    ProbeResult,
    RingPolicy,
    Target,
    ToolSpec,
    new_id,
)
from envrec.guard import LOOP_ACTIVE
from envrec.loop.menu import MenuEntry, build_menu, eligible_tools
from envrec.loop.prompts import R45_ACTIONS, R45_PROMPT, RETRY_HINT, SYSTEM_PROMPT
from envrec.loop.view import build_view, expand_fact_text
from envrec.model import ModelAdapter, request_action
from envrec.policy import PolicyState, decide
from envrec.rings import close_ring
from envrec.store import Store

Approver = Callable[[ToolSpec, Target], bool]
HumanAsker = Callable[[str], str | None]


@dataclass
class RunConfig:
    max_steps: int = 25
    max_seconds: float = 300.0
    playbook_dir: Path | None = None
    repair: bool = True  # read two common form slips as call_tool, always counted and logged


@dataclass
class RunSummary:
    run_id: str
    steps: int = 0
    stop_reason: str = ""
    model_calls: int = 0
    invalid_replies: int = 0
    fallbacks: int = 0
    repaired: int = 0
    rejected_actions: int = 0
    policy_denied: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model_latency_s: float = 0.0
    elapsed_s: float = 0.0
    tools_run: list[str] = field(default_factory=list)
    ring_decisions: dict[str, str] = field(default_factory=dict)  # target id -> deepen/extend/stop
    escalated_turns: list[dict] = field(default_factory=list)


class Loop:
    def __init__(self, store: Store, adapters: list[Adapter], model: ModelAdapter, *,
                 config: RunConfig | None = None, approve: Approver | None = None,
                 ask_human: HumanAsker | None = None, rung45: Any = None, mission: Any = None, escalator: Any = None) -> None:
        self.store, self.adapters, self.model = store, adapters, model
        self.config = config or RunConfig()
        self._approve, self._ask_human = approve, ask_human
        self.rung45, self.mission, self.escalator = rung45, mission, escalator
        self.policy_state = PolicyState()
        self.run_id = new_id("run")
        self.summary = RunSummary(run_id=self.run_id)
        self._unknown_reasons: dict[str, str] = {}
        self._asked: set[str] = set()
        self._visited: list[Target] = []

    # ---- events --------------------------------------------------------------------------------

    def _log(self, actor: Actor, type: str, payload: dict[str, Any] | None = None) -> Event:
        return self.store.append_event(Event(actor=actor, type=type, payload=payload or {}, run_id=self.run_id))

    def _emit_model_event(self, type: str, payload: dict[str, Any]) -> None:
        if type == "model_call":
            self.summary.model_calls += 1
        elif type == "model_invalid":
            self.summary.invalid_replies += 1
        elif type == "model_fallback":
            self.summary.fallbacks += 1
        elif type == "model_repaired":
            self.summary.repaired += 1
        self._log(Actor.loop, type, payload)

    # ---- the run ---------------------------------------------------------------------------------

    async def run(self, target: Target) -> RunSummary:
        started = time.monotonic()
        loop_token = LOOP_ACTIVE.set(True)  # human-only code (enrollment) refuses to run while the loop is active
        profile = self.model.profile
        current = target
        self._visit(current)
        last_result = "nothing run yet"
        self._log(Actor.loop, "run_start", {
            "target": current.id, "model_profile": profile.name, "view_budget_facts": profile.view_budget_facts,
            "menu_max": profile.menu_max, "max_steps": self.config.max_steps})
        stop = "step_cap"
        try:
            for _ in range(self.config.max_steps):
                remaining = self.config.max_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    stop = "time_cap"
                    break
                current = self.store.get_target(current.id) or current  # tools may update it (signature)
                menu = build_menu(self.adapters, current, self.store, profile.menu_max, self.policy_state)
                if not menu:
                    nxt = self._next_ring_target(current)
                    if nxt is not None and profile.ring_policy == RingPolicy.fixed_order:
                        # small-model policy: code walks the rings in fixed order, the model never decides this
                        self._log(Actor.loop, "ring_extended", {"from": current.id, "to": nxt.id,
                                                                "ring": nxt.ring, "by": "code"})
                        self._visit(nxt)
                        current, last_result = nxt, f"moved to ring {nxt.ring}: {nxt.kind.value} {nxt.handle}"
                        continue
                    if nxt is None and not self._wants_human(current):
                        stop = "menu_empty"
                        break
                view = build_view(self.store, current, profile, menu, last_result, self._unknown_reasons,
                                  self.config.playbook_dir, R45_ACTIONS if self.rung45 else "",
                                  self.mission.render() if self.mission else "")
                self._log(Actor.loop, "view_built", {"target": current.id, "menu": [e.name for e in menu],
                                                     "view_chars": len(view)})
                fallback = Action(action=ActionType.record_unknown, key=f"{KIND_PREFIX[current.kind.value]}.identity",
                                  question="the model gave no valid action", why="fallback after two invalid replies")
                try:
                    res = await asyncio.wait_for(
                        request_action(self.model, SYSTEM_PROMPT + (R45_PROMPT if self.rung45 else ""), view, fallback=fallback, emit=self._emit_model_event,
                                       repair_tools={e.name for e in menu} if self.config.repair else None,
                                       retry_hint=RETRY_HINT,
                                       pick=(lambda: self.escalator.pick(self.model)) if self.escalator else None,
                                       observe=self.escalator.observe if self.escalator else None),
                        remaining)
                except asyncio.TimeoutError:
                    stop = "time_cap"
                    break
                self.summary.steps += 1
                if self.escalator:
                    self.escalator.start_turn(self.summary.steps)
                self.summary.prompt_tokens += res.prompt_tokens
                self.summary.completion_tokens += res.completion_tokens
                self.summary.model_latency_s += res.latency_s
                action = res.action
                if self.escalator and action.action == ActionType.ask_human and not res.used_fallback:
                    strong = self.escalator.route_rung6()
                    second = await request_action(strong, SYSTEM_PROMPT + (R45_PROMPT if self.rung45 else ""), view, fallback=fallback,
                                                  emit=self._emit_model_event)
                    action = second.action
                self._log(Actor.loop, "action_chosen", {"action": action.action.value, "tool": action.tool,
                                                        "fallback": res.used_fallback, "why": action.why})
                if action.action == ActionType.done:
                    stop = "done"
                    break
                new_current, last_result = await self._apply(action, current, menu, res.used_fallback)
                current = new_current
        finally:
            LOOP_ACTIVE.reset(loop_token)
            if self.escalator:
                for e in self.escalator.escalated:
                    self._log(Actor.loop, "escalated", e)
                self.summary.escalated_turns = list(self.escalator.escalated)
                self.escalator.close()
            self.summary.stop_reason = stop
            self.summary.elapsed_s = time.monotonic() - started
            self._close_rings()
            self._log(Actor.loop, "run_end", {"stop_reason": stop, "steps": self.summary.steps,
                                              "tools_run": self.summary.tools_run,
                                              "ring_decisions": self.summary.ring_decisions})
        return self.summary

    # ---- one action ------------------------------------------------------------------------------

    async def _apply(self, action: Action, current: Target, menu: list[MenuEntry],
                     fallback: bool = False) -> tuple[Target, str]:
        t = action.action
        if t == ActionType.call_tool:
            return current, await self._call_tool(action, current, menu)
        if t == ActionType.expand_fact:
            return current, self._expand(action, current)
        if t == ActionType.record_unknown:
            return current, self._record_unknown(action, current, fallback)
        if t == ActionType.ask_human:
            return current, self._ask(action, current)
        if t == ActionType.extend_to:
            return self._extend(action, current)
        if t == ActionType.research and self.rung45:
            evt = self._log(Actor.loop, "research", {"target": current.id, "rung": 4, "question": action.question})
            return current, self.rung45.research(self.store, current, action.question or "", evt.id)
        if t == ActionType.propose_probe and self.rung45:
            evt = self._log(Actor.loop, "probe_proposed", {"target": current.id, "rung": 5,
                                                           "code_chars": len(action.code or "")})
            return current, await self.rung45.propose(self.store, current, action.code or "",
                                                      action.test_plan or "", evt.id)
        return current, self._reject(action, f"{t.value} is not available in this phase")

    def _reject(self, action: Action, reason: str) -> str:
        self.summary.rejected_actions += 1
        self._log(Actor.loop, "action_rejected", {"action": action.action.value, "tool": action.tool, "reason": reason})
        return f"rejected: {reason}"

    async def _call_tool(self, action: Action, current: Target, menu: list[MenuEntry]) -> str:
        entry = next((e for e in menu if e.name == action.tool), None)
        if entry is None:
            return self._reject(action, f"{action.tool!r} is not on the menu")
        target = self.store.get_target(action.target_id) if action.target_id else current
        if target is None:
            return self._reject(action, f"unknown target {action.target_id!r}")

        verdict = decide(entry.spec, target, self.policy_state)
        self._log(Actor.policy, "policy_decision", {"tool": entry.name, "target": target.id,
                                                    "decision": verdict.decision.value, "reason": verdict.reason})
        if verdict.decision == Decision.deny:
            self.summary.policy_denied += 1
            return f"{entry.name} denied by policy: {verdict.reason}"
        if verdict.decision == Decision.needs_approval:
            granted = bool(self._approve and self._approve(entry.spec, target))
            self._log(Actor.human, "approval", {"tool": entry.name, "target": target.id, "granted": granted})
            if not granted:
                return f"{entry.name} needs human approval and it was not granted"
            self.policy_state = PolicyState(approved=self.policy_state.approved | {(entry.name, target.id)})

        run_evt = self._log(Actor.loop, "tool_run", {"tool": entry.name, "target": target.id,
                                                     "rung": entry.spec.rung, "args": sorted(action.args)})
        try:
            result = await entry.adapter.run(entry.name, dict(action.args),
                                             ToolContext(store=self.store, target=target, event_id=run_evt.id))
        except Exception as exc:  # an adapter bug must not end the run
            result = ProbeResult(ok=False, error=f"{type(exc).__name__}: {str(exc)[:200]}")
        self.summary.tools_run.append(entry.name)
        return self._record_result(entry.name, result)

    def _record_result(self, tool: str, result: ProbeResult) -> str:
        if not result.ok:
            self._log(Actor.loop, "tool_result", {"tool": tool, "ok": False, "error": result.error})
            return f"{tool} failed: {result.error}"
        for fact in result.facts:
            self.store.write_fact(fact)
        for u in result.unknowns:
            self._unknown_reasons[u.key] = u.question or "could not be determined"
        unknown = sum(1 for f in result.facts if f.status == FactStatus.unknown)
        self._log(Actor.loop, "tool_result", {
            "tool": tool, "ok": True, "facts": [f.key for f in result.facts], "unknown": unknown,
            "raw": result.raw_ref.sha256[:12] if result.raw_ref else None})
        return f"{tool} ok: {len(result.facts)} facts ({unknown} unknown)"

    def _expand(self, action: Action, current: Target) -> str:
        fact = (self.store.get_fact(action.fact_id) if action.fact_id else None) or (
            self.store.current_fact(current.id, action.key) if action.key else None)
        if fact is None:
            return self._reject(action, "no such fact to expand")
        self._log(Actor.loop, "fact_expanded", {"key": fact.key})
        return expand_fact_text(fact)

    def _record_unknown(self, action: Action, current: Target, fallback: bool = False) -> str:
        key = action.key or f"{KIND_PREFIX[current.kind.value]}.identity"
        self.store.write_fact(Fact(target_id=current.id, key=key, created_by=CreatedBy.model))
        self._unknown_reasons[key] = action.question or "recorded unknown by the model"
        self._log(Actor.loop, "unknown_recorded", {"key": key, "question": action.question,
                                                   "target": current.id, "fallback": fallback})
        return f"recorded {key} as unknown"

    def _wants_human(self, current: Target) -> bool:
        """With no tools left, still let the model ask a human about open unknowns, once per target."""
        if current.id in self._asked:
            return False
        return any(f.status == FactStatus.unknown for f in self.store.current_facts(current.id))

    def _ask(self, action: Action, current: Target) -> str:
        key = action.key or f"{KIND_PREFIX[current.kind.value]}.identity"
        question = action.question or f"What is {key}?"
        self._asked.add(current.id)
        evt = self._log(Actor.human, "human_question", {"key": key, "question": question, "target": current.id})
        answer = self._ask_human(question) if self._ask_human else None
        if not answer:
            return "asked the human; no answer is available"
        self.store.write_fact(Fact(
            target_id=current.id, key=key, value=answer[:200], status=FactStatus.tested,
            created_by=CreatedBy.human,
            evidence=[Evidence(provider="human", probe="ask_human", event_id=evt.id, confidence=1.0,
                               limitations=["stated by the human, not measured"])]))
        self._log(Actor.human, "human_answer", {"key": key})
        return f"the human answered for {key}"

    def _next_ring_target(self, current: Target, ring: int | None = None) -> Target | None:
        """The nearest unvisited target on a further ring that hangs off the current one."""
        seen = {v.id for v in self._visited}
        cands = [t for t in self.store.list_targets()
                 if t.id not in seen and t.ring > current.ring and t.parent in (None, current.id)
                 and (ring is None or t.ring == ring)]
        return min(cands, key=lambda t: t.ring, default=None)

    def _extend(self, action: Action, current: Target) -> tuple[Target, str]:
        wanted = action.ring
        nxt = self._next_ring_target(current, wanted)
        if nxt is None:
            return current, self._reject(action, f"no target on ring {wanted} to move to")
        self._log(Actor.loop, "ring_extended", {"from": current.id, "to": nxt.id, "ring": wanted})
        self._visit(nxt)
        return nxt, f"moved to ring {wanted}: {nxt.kind.value} {nxt.handle}"

    # ---- rings -------------------------------------------------------------------------------------

    def _visit(self, target: Target) -> None:
        if all(v.id != target.id for v in self._visited):
            self._visited.append(target)

    def _expected_keys(self, target: Target) -> list[str]:
        keys = {k for e in eligible_tools_all(self.adapters, target) for k in e.produces}
        return sorted(keys)

    def _close_rings(self) -> None:
        for target in self._visited:
            further = any(t.ring > target.ring for t in self.store.list_targets())
            cov = close_ring(self.store, target, target.ring, self._expected_keys(target), further_rings=further)
            self.summary.ring_decisions[target.id] = cov.decision.value
            self._log(Actor.loop, "ring_closed", {"target": target.id, "ring": target.ring,
                                                  "checked": len(cov.checked), "unknown": cov.unknown,
                                                  "decision": cov.decision.value})


def eligible_tools_all(adapters: list[Adapter], target: Target) -> list[ToolSpec]:
    """Every tool that could ever apply to this target (ignores what has already run)."""
    return [s for a in adapters for s in a.tool_specs() if target.kind in s.target_kinds and s.ring <= target.ring]

"""The loop (file 06, section A): view -> menu -> one Action -> validate -> policy -> run -> record -> stop check.

Plain asyncio, no agent framework. The model only picks from a menu and fills a form; code
runs everything. Raw output goes to the store and never into a prompt.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
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
from envrec.discovery.sanitize import Salts
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
    protocols: Any = None  # a ProtocolSet: when given, the loop enforces the protocol of each target kind (menu per step, evidence to advance)
    library: Any = None  # a skills Library: chosen skills put their body in the view for one turn
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
    protocol_refusals: int = 0  # advance requests refused for lacking evidence
    protocol_history: dict[str, list[dict]] = field(default_factory=dict)  # target id -> the steps it went through, and by whom (model or code)


class _Finished(Exception):
    pass


class Loop:
    def __init__(self, store: Store, adapters: list[Adapter], model: ModelAdapter, *,
                 config: RunConfig | None = None, approve: Approver | None = None,
                 ask_human: HumanAsker | None = None, workbench: Any = None, mission: Any = None, escalator: Any = None) -> None:
        self.store, self.adapters, self.model = store, adapters, model
        self.config = config or RunConfig()
        self._approve, self._ask_human = approve, ask_human
        self.workbench, self.mission, self.escalator = workbench, mission, escalator
        self.policy_state = PolicyState()
        self.run_id = new_id("run")
        self.summary = RunSummary(run_id=self.run_id)
        self._unknown_reasons: dict[str, str] = {}
        self._asked: set[str] = set()
        self._visited: list[Target] = []
        self._proto_runs: dict[str, Any] = {}
        self._skill_note = ""

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
            if await self._first_rung(current):  # rung 1: a stored skill recognised it; no model was asked anything
                stop = "skill_reused"
                raise _Finished
            for _ in range(self.config.max_steps):
                remaining = self.config.max_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    stop = "time_cap"
                    break
                current = self.store.get_target(current.id) or current  # tools may update it (signature)
                proto = self._proto(current)
                if proto is not None and proto.terminal and self._next_ring_target(current) is None:
                    stop = "protocol_done"
                    break
                allow = (lambda e, p=proto: p.allows_entry(e.name, list(e.spec.tags), self._skill_protocol(e.name))) if proto is not None else None
                menu = build_menu(self.adapters, current, self.store, profile.menu_max, self.policy_state, allow)
                if not menu and proto is not None and not proto.terminal and profile.ring_policy == RingPolicy.fixed_order and self._code_advance(proto, current):
                    last_result = f"protocol step is now {proto.state} (advanced by code: nothing left to do in the previous step)"
                    continue
                if not menu:
                    nxt = self._next_ring_target(current)
                    if nxt is not None and profile.ring_policy == RingPolicy.fixed_order:
                        # small-model policy: code walks the rings in fixed order, the model never decides this
                        self._log(Actor.loop, "ring_extended", {"from": current.id, "to": nxt.id,
                                                                "ring": nxt.ring, "by": "code"})
                        self._visit(nxt)
                        current, last_result = nxt, f"moved to ring {nxt.ring}: {nxt.kind.value} {nxt.handle}"
                        continue
                    if nxt is None and not self._wants_human(current) and not (proto is not None and not proto.terminal):
                        stop = "menu_empty"
                        break
                view = build_view(self.store, current, profile, menu, last_result, self._unknown_reasons,
                                  proto.card() if proto is not None else "",
                                  (R45_ACTIONS if self.workbench else "") + (" · advance(to)" if proto is not None else ""),
                                  self.mission.render() if self.mission else "", self._skill_note)
                self._skill_note = ""
                self._log(Actor.loop, "view_built", {"target": current.id, "menu": [e.name for e in menu],
                                                     "view_chars": len(view)})
                fallback = Action(action=ActionType.record_unknown, key=f"{KIND_PREFIX[current.kind.value]}.identity",
                                  question="the model gave no valid action", why="fallback after two invalid replies")
                try:
                    res = await asyncio.wait_for(
                        request_action(self.model, SYSTEM_PROMPT + (R45_PROMPT if self.workbench else ""), view, fallback=fallback, emit=self._emit_model_event,
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
                    second = await request_action(strong, SYSTEM_PROMPT + (R45_PROMPT if self.workbench else ""), view, fallback=fallback,
                                                  emit=self._emit_model_event)
                    action = second.action
                self._log(Actor.loop, "action_chosen", {"action": action.action.value, "tool": action.tool,
                                                        "fallback": res.used_fallback, "why": action.why})
                if action.action == ActionType.done:
                    stop = "done"
                    break
                new_current, last_result = await self._apply(action, current, menu, res.used_fallback)
                current = new_current
        except _Finished:
            pass
        finally:
            LOOP_ACTIVE.reset(loop_token)
            if self.escalator:
                for e in self.escalator.escalated:
                    self._log(Actor.loop, "escalated", e)
                self.summary.escalated_turns = list(self.escalator.escalated)
                self.escalator.close()
            self.summary.stop_reason = stop
            self.summary.protocol_history = {tid: r.history for tid, r in self._proto_runs.items()}
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
        if t == ActionType.advance:
            return current, self._advance(action, current)
        proto = self._proto(current)
        if proto is not None and not proto.allows_action(t.value):
            self.summary.protocol_refusals += 1
            self._log(Actor.loop, "protocol_refused", {"target": current.id, "state": proto.state, "action": t.value, "reason": "not allowed in this step"})
            return current, self._reject(action, f"{t.value} is not allowed in protocol step {proto.state}")
        if proto is not None and t.value in ("research", "propose_skill", "ask_human"):
            proto.taken.add(t.value)
        if t == ActionType.research and self.workbench:
            evt = self._log(Actor.loop, "research", {"target": current.id, "rung": 4, "question": action.question})
            return current, self.workbench.research(self.store, current, action.question or "", evt.id)
        if t == ActionType.propose_skill and self.workbench:
            evt = self._log(Actor.loop, "skill_proposed", {"target": current.id, "rung": 5, "draft_chars": len(json.dumps(action.skill_draft or {}))})
            return current, await self.workbench.propose(self.store, current, action.skill_draft or {}, evt.id)
        return current, self._reject(action, f"{t.value} is not available in this phase")

    # ---- rung 1: the stored skills ------------------------------------------------------------------

    async def _first_rung(self, target: Target) -> bool:
        """Before any model call: run the identify skills for this kind of target (they produce the facts a signature can match, such as a banner or a vendor id), then offer
        the stored skills whose signature matches. A matching skill the human approved and that was verified here runs now, with no model call. Returns True when a stored
        skill produced facts for this target (the question is answered; the run can end)."""
        reg = next((a for a in self.adapters if getattr(a, "library", None) is not None and hasattr(a, "run_skill_now")), None)
        lib = self.config.library or (reg.library if reg else None)
        if reg is None or lib is None:
            return False
        ctx = ToolContext(store=self.store, target=target, event_id=None)
        for sk in lib.runnable():
            m = sk.meta
            if m.identify and m.signature.kind in (None, target.kind.value) and ("any" in m.platforms or reg.platform in m.platforms):
                evt = self._log(Actor.loop, "first_rung_identify", {"target": target.id, "skill": sk.name})
                self._record_result(f"skill.{sk.key}", reg.run_skill_now(sk, ToolContext(store=self.store, target=target, event_id=evt.id), {}))
        facts = {f.key: f.value for f in self.store.current_facts(target.id) if f.status != FactStatus.unknown}
        reused = False
        matches = [s for s in lib.match(target, facts, reg.platform) if s.meta.provenance == "generated"]
        for sk in matches:
            before = self.summary.model_calls
            evt = self._log(Actor.loop, "skill_reuse", {"target": target.id, "skill": sk.name})
            res = reg.run_skill_now(sk, ToolContext(store=self.store, target=target, event_id=evt.id), {})
            self._log(Actor.loop, "skill_reused", {"target": target.id, "skill": sk.name, "ok": res.ok, "facts": len(res.facts), "model_calls": self.summary.model_calls - before,
                                                   "error": res.error})
            self._record_result(f"skill.{sk.key}", res)
            reused = reused or (res.ok and any(f.status != FactStatus.unknown for f in res.facts))
            if res.ok:
                break
        ev = Evidence(provider="skill-library", probe="signature match", confidence=1.0, limitations=["a measured lookup of the stored skills, not a claim about the device"])
        self.store.write_fact(Fact(target_id=target.id, key="skill.match", value=(matches[0].name if reused else False), status=FactStatus.tested, evidence=[ev], created_by=CreatedBy.loop))
        return reused

    # ---- protocols ---------------------------------------------------------------------------------

    def _proto(self, target: Target):
        ps = self.config.protocols
        if ps is None:
            return None
        if target.id not in self._proto_runs:
            run = ps.start(target.kind.value)
            if run is None:
                return None
            self._proto_runs[target.id] = run
            self._log(Actor.loop, "protocol_start", {"target": target.id, "protocol": run.id, "state": run.state})
        return self._proto_runs[target.id]

    def _skill_protocol(self, entry_name: str) -> str | None:
        lib = self.config.library
        s = lib.get(entry_name[6:]) if lib is not None and entry_name.startswith("skill.") else None
        return s.meta.protocol if s is not None else None

    def _evidence(self, target: Target, proto) -> Any:
        from envrec.protocols import EvidenceView
        tags = {s.name: set(s.tags) for a in self.adapters for s in a.tool_specs()}
        return EvidenceView({f.key: f for f in self.store.current_facts(target.id)}, set(self.store.tools_run(target.id)), tags, proto.taken)

    def _advance(self, action: Action, current: Target) -> str:
        proto = self._proto(current)
        if proto is None:
            return self._reject(action, "no protocol is active for this target")
        chk = proto.check(action.to or "", self._evidence(current, proto))
        if not chk.ok:
            self.summary.protocol_refusals += 1
            msg = proto.refuse(action.to or "", chk.missing)
            self._log(Actor.loop, "protocol_refused", {"target": current.id, "from": proto.state, "to": action.to, "missing": chk.missing, "count": proto.refusals})
            return msg
        before = proto.state
        proto.move(action.to or "", "model")
        self._log(Actor.loop, "protocol_state", {"target": current.id, "from": before, "to": proto.state, "by": "model"})
        return f"advanced from {before} to {proto.state}"

    def _code_advance(self, proto, current: Target) -> bool:
        """A small model is never left stuck in a step with no tool left: when the evidence for the exit is already in, code moves on; when only an unknown is missing
        (a probe was tried and could not tell), code records that unknown and moves on. Anything else (a tool or an action still owed) is left to the model."""
        ev = self._evidence(current, proto)
        nxt = proto.ready(ev)
        if nxt is None:
            missing = [k for k in proto.missing_for_exit(ev) if k not in ev.facts]
            for k in missing:
                self.store.write_fact(Fact(target_id=current.id, key=k, created_by=CreatedBy.loop))
                self._unknown_reasons[k] = "no tool left in this protocol step could determine it"
                self._log(Actor.loop, "unknown_recorded", {"key": k, "target": current.id, "by": "code", "state": proto.state})
            nxt = proto.ready(self._evidence(current, proto)) if missing else None
        if nxt is None:
            return False
        before = proto.state
        proto.move(nxt, "code")
        self._log(Actor.loop, "protocol_state", {"target": current.id, "from": before, "to": nxt, "by": "code"})
        return True

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
        if entry.name.startswith("skill.") and self.config.library is not None:
            self._skill_note = self.config.library.body_for(entry.name[6:])[:6000]  # level 2: the body of the chosen skill, for the next turn only
        return self._record_result(entry.name, result)

    def _record_result(self, tool: str, result: ProbeResult) -> str:
        if not result.ok:
            self._log(Actor.loop, "tool_result", {"tool": tool, "ok": False, "error": result.error})
            return f"{tool} failed: {result.error}"
        from envrec.privacy.gate import PrivacyGate
        PrivacyGate(self.store, None, Salts(), tool).write_facts(result.facts)  # the one fact boundary
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

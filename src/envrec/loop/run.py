"""The loop (file 06, section A): view -> menu -> one Action -> validate -> policy -> run -> record -> stop check.

Plain asyncio, no agent framework. The model only picks from a menu and fills a form; code
runs everything. Raw output goes to the store and never into a prompt.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
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
    Target,
    ToolSpec,
    new_id,
)
from envrec.discovery.sanitize import Salts, quoted
from envrec.guard import LOOP_ACTIVE
from envrec.loop.menu import MenuEntry, build_menu, eligible_tools
from envrec.loop.prompts import R45_ACTIONS, R45_PROMPT, RETRY_HINT, SYSTEM_PROMPT
from envrec.loop.view import build_view, expand_fact_text, way_forward_text
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
    stop_at: str | None = None  # a protocol step name: the run ends there as a typed `stop_state_reached` (a read-only run that must not go on to write anything)
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
    owner_wait_s: float = 0.0  # time spent waiting for the owner (approvals, answers): not counted against the time cap
    tools_run: list[str] = field(default_factory=list)
    ring_decisions: dict[str, str] = field(default_factory=dict)  # target id -> deepen/extend/stop
    escalated_turns: list[dict] = field(default_factory=list)
    repeat_refusals: int = 0  # the same tool asked again with the same arguments
    protocol_refusals: int = 0  # advance requests refused for lacking evidence
    walk_for_model: bool = False  # the experimental dial was on: code walked steps for the model, and said so in the log
    protocol_history: dict[str, list[dict]] = field(default_factory=dict)  # target id -> the steps it went through, and by whom (model or code)
    hangs: int = 0  # calls (a model request or a tool) that did not answer within their hard limit
    empty_menu_turns: int = 0  # turns the model was asked with an empty menu, and why (below): never silent
    empty_menu_causes: dict[str, int] = field(default_factory=dict)


MODEL_CALL_TIMEOUT_S = 240.0  # one model call; past it the run ends as a typed HANG (the model server is not answering), never a silent wait
MAX_DONE_REFUSALS = 3  # the model said done this many times in an unfinished protocol: the run ends as `refused_done_loop` (incomplete, never success)
NO_PROGRESS_TURNS = 5  # turns in a row that add no fact, move no step and run no tool end the run as `no_progress`


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
        self._done_refused = 0
        self._blocked = False
        self._last_ok_tool: str | None = None
        self._asked_before: set[tuple[str, str, str]] = set()  # (target, tool, arguments) already asked
        self._stuck: dict[str, int] = {}  # target -> consecutive turns with nothing new
        self._skill_note = ""
        self._idle = 0
        self._last_progress: tuple | None = None
        self._waited = 0.0

    def _owner_wait(self) -> float:
        """Seconds the run has waited for the owner so far: the loop's own questions and approvals plus those the adapters' tools asked."""
        return self._waited + sum(getattr(a, "owner_wait_s", 0.0) for a in self.adapters)

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
            "menu_max": profile.menu_max, "max_steps": self.config.max_steps, "walk_for_model": profile.walk_for_model})
        self.summary.walk_for_model = profile.walk_for_model
        stop = "step_cap"
        try:
            if await self._first_rung(current):  # rung 1: a stored skill recognised it; no model was asked anything
                stop = "skill_reused"
                raise _Finished
            for _ in range(self.config.max_steps):
                if self._blocked:
                    stop = "blocked_needs_human"
                    break
                remaining = self.config.max_seconds - (time.monotonic() - started - self._owner_wait())
                if remaining <= 0:
                    stop = "time_cap"
                    break
                current = self.store.get_target(current.id) or current  # tools may update it (signature)
                proto = self._proto(current)
                if proto is not None and self.config.stop_at and proto.state == self.config.stop_at:
                    stop = "stop_state_reached"
                    break
                if proto is not None and proto.terminal and self._next_ring_target(current) is None:
                    stop = "protocol_done"
                    break
                walk = profile.walk_for_model  # the experimental dial: code walks the protocol for the model (every forced step is SCRIPT)
                if walk and proto is not None and not proto.terminal:
                    ready = proto.ready(self._evidence(current, proto))
                    if ready is not None:  # dial on: once a step's evidence is in, code moves on for the model. A forced step: tagged SCRIPT, counted, and the run says "dial on".
                        before = proto.state
                        proto.move(ready, "code")
                        self._log(Actor.loop, "protocol_state", {"target": current.id, "from": before, "to": ready, "by": "code"})
                        last_result = f"protocol step {before} is finished; now {ready} (moved by code)"
                        continue
                live = proto is not None and not proto.terminal
                ev = self._evidence(current, proto) if live else None
                allow = (lambda e, p=proto, ev=ev: p.allows_entry(e.name, list(e.spec.tags), self._skill_protocol(e.name)) and (ev is None or p.closing_open(e.name, list(e.spec.produces), ev))) if proto is not None else None
                need = proto.missing_facts(ev) if live else None  # (exit, fact keys it lacks): shown to the model, and the tools that make those keys stay inside the capped menu
                menu = build_menu(self.adapters, current, self.store, profile.menu_max, self.policy_state, allow, set(need[1]) if need else None)
                forward = proto.ready_all(ev) if live else []
                cands = self._candidates(current)
                if not menu:
                    nxt = self._next_ring_target(current)
                    if nxt is not None and walk:
                        # small-model policy: code walks the rings in fixed order, the model never decides this
                        self._log(Actor.loop, "ring_extended", {"from": current.id, "to": nxt.id,
                                                                "ring": nxt.ring, "by": "code"})
                        self._visit(nxt)
                        current, last_result = nxt, f"moved to ring {nxt.ring}: {nxt.kind.value} {nxt.handle}"
                        continue
                    cause = self._empty_menu_cause(current, proto, forward, cands, nxt)
                    self.summary.empty_menu_turns += 1
                    self.summary.empty_menu_causes[cause] = self.summary.empty_menu_causes.get(cause, 0) + 1
                    self._log(Actor.loop, "menu_empty", {"target": current.id, "state": proto.state if proto is not None else None, "cause": cause, "forward": forward})
                    if cause == "stuck":  # nothing on the menu, no way forward, nobody left to ask: a typed end, not a loop of dead turns
                        stop = "menu_empty"
                        break

                view = build_view(self.store, current, profile, menu, last_result, self._unknown_reasons,
                                  proto.card() if proto is not None else "",
                                  (R45_ACTIONS if self.workbench else "") + (" · advance(to)" if proto is not None else "") + (" · extend_to(target_id)" if cands else ""),
                                  self.mission.render() if self.mission else "", self._skill_note or self._kept_skills(proto), cands, forward,
                                  self._missing_text(current, need, menu))
                self._skill_note = ""
                self._log(Actor.loop, "view_built", {"target": current.id, "menu": [e.name for e in menu],
                                                     "forward": forward, "view_chars": len(view), "view": view})  # the text itself: the audit of a run needs what the model was shown, not its digest
                fallback = Action(action=ActionType.record_unknown, key=f"{KIND_PREFIX[current.kind.value]}.identity",
                                  question="the model gave no valid action", why="fallback after two invalid replies")
                try:
                    res = await asyncio.wait_for(
                        request_action(self.model, SYSTEM_PROMPT + (R45_PROMPT if self.workbench else ""), view, fallback=fallback, emit=self._emit_model_event,
                                       repair_tools={e.name for e in menu} if self.config.repair else None,
                                       retry_hint=RETRY_HINT,
                                       pick=(lambda: self.escalator.pick(self.model)) if self.escalator else None,
                                       observe=self.escalator.observe if self.escalator else None),
                        min(remaining, MODEL_CALL_TIMEOUT_S))
                except asyncio.TimeoutError:
                    if remaining <= MODEL_CALL_TIMEOUT_S:
                        stop = "time_cap"
                    else:
                        stop = "HANG"
                        self.summary.hangs += 1
                        self._log(Actor.loop, "hang", {"kind": "model", "limit_s": MODEL_CALL_TIMEOUT_S, "model": profile.model_id})
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
                                                        "fallback": res.used_fallback, "why": action.why,
                                                        "view_digest": hashlib.sha256(view.encode()).hexdigest()[:16], "menu": [e.name for e in menu],
                                                        "raw_reply": res.raw_reply, "validation": res.validation,
                                                        "model": profile.model_id, "profile": profile.name})
                if action.action == ActionType.done:
                    if proto is not None and not proto.terminal:
                        # the protocol has steps left: done is refused every time. A model that keeps asking ends the run as incomplete, never as a success.
                        self._done_refused += 1
                        self.summary.protocol_refusals += 1
                        ev_now = self._evidence(current, proto)
                        need, open_to = proto.missing_facts(ev_now), proto.ready_all(ev_now)
                        clause = self._missing_text(current, need, menu)
                        why_not = ("; this step's evidence is in, the way forward is to advance to " + " or ".join(open_to)) if open_to else (f"; still missing: {clause}" if clause else "")
                        self._log(Actor.loop, "protocol_refused", {"target": current.id, "state": proto.state, "action": "done", "code": "protocol_not_ended", "count": self._done_refused,
                                                                   "reason": "the protocol has not ended" + why_not, "forward": open_to,
                                                                   "missing": need[1] if need else [], "produced_by": self._producers(current, need[1]) if need else {}})
                        last_result = (f"refused: done is not available in step {proto.state}; the protocol has not ended. "
                                       + (way_forward_text(open_to) if open_to else f"Still missing: {clause}" if clause else proto.card().splitlines()[-1]))
                        if self._done_refused >= MAX_DONE_REFUSALS:
                            stop = "refused_done_loop"
                            break
                        continue
                    stop = "done"
                    break
                self._last_ok_tool = None
                new_current, last_result = await self._apply(action, current, menu, res.used_fallback)
                if proto is not None and not proto.terminal and self._last_ok_tool in proto.current.close_on:
                    to = proto.ready(self._evidence(current, proto))  # the model chose the closing tool and it succeeded: if an exit's evidence is in, the guard takes it (GUARD, not MODEL)
                    if to is not None:
                        before = proto.state
                        proto.move(to, "evidence")
                        self._log(Actor.loop, "protocol_state", {"target": current.id, "from": before, "to": to, "by": "evidence", "closed_by": self._last_ok_tool})
                        last_result += f"; the step is complete: {before} -> {to}"
                if forward and last_result.startswith("rejected:"):  # a refused action while this step's evidence is in: the next view says where the open way is (the model still has to take it)
                    last_result += ". " + way_forward_text(forward)
                current = new_current
                sig = self._progress(current)
                self._idle = self._idle + 1 if sig == self._last_progress else 0
                self._last_progress = sig
                if self._idle >= NO_PROGRESS_TURNS:
                    stop = "no_progress"
                    break
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
            self.summary.owner_wait_s = round(self._owner_wait(), 1)
            self.summary.elapsed_s = time.monotonic() - started - self._owner_wait()  # the run's own time; the owner's thinking is reported separately
            for adapter in self.adapters:
                adapter.close()  # what an adapter left open (a link being served) is shut down, and recorded, before the run is declared over
            self._close_rings()
            self._log(Actor.loop, "run_end", {"stop_reason": stop, "steps": self.summary.steps,
                                              "tools_run": self.summary.tools_run,
                                              "ring_decisions": self.summary.ring_decisions})
        return self.summary

    def _producers(self, target: Target, keys: list[str]) -> dict[str, list[str]]:
        """For each fact key: the tools registered for this kind of target that declare they produce it (a lookup in the specs; nothing is run)."""
        out: dict[str, list[str]] = {k: [] for k in keys}
        for a in self.adapters:
            for s in a.tool_specs():
                if target.kind in s.target_kinds:
                    for k in keys:
                        if k in s.produces:
                            out[k].append(s.name)
        return out

    def _missing_text(self, target: Target, need: tuple[str, list[str]] | None, menu: list[MenuEntry]) -> str:
        """`to move on to <exit> it needs KEY (produced by TOOL, on the menu now)`: the key the exit lacks, the tool that makes it, and whether the model can pick it this turn."""
        if not need:
            return ""
        on_menu = {e.name for e in menu}
        parts = []
        for key, names in self._producers(target, need[1]).items():
            if not names:
                parts.append(f"{key} (no tool for this target produces it)")
            else:
                parts.append(f"{key} (produced by {' or '.join(names)}, {'on the menu now' if on_menu & set(names) else 'not on the menu now'})")
        return f"to move on to {need[0]} it needs " + "; ".join(parts)

    def _empty_menu_cause(self, current: Target, proto, forward: list[str], cands: list[Target], nxt) -> str:
        """Why the menu is empty, and whether the model still has anything to do. `way_forward`: the step's evidence is in and the model's move is to advance. `actions`: the model can still
        ask the human, move to a candidate, or take an action the step lists. `stuck`: none of those; the run ends as `menu_empty` instead of asking the model again and again."""
        if forward:
            return "way_forward"
        if proto is not None and proto.terminal:
            return "protocol_ended" if nxt is not None or cands else "stuck"
        can_ask = self._wants_human(current) if proto is None else current.id not in self._asked  # without a protocol, only an open unknown is worth a question
        if can_ask or cands or nxt is not None or (proto is not None and any(a in ("research", "propose_skill") for a in proto.current.actions) and self.workbench):
            return "actions"
        return "stuck"

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
            proto.taken[t.value] = proto.taken.get(t.value, 0) + 1
        if t == ActionType.research and self.workbench:
            evt = self._log(Actor.loop, "research", {"target": current.id, "rung": 4, "question": action.question})
            return current, self.workbench.research(self.store, current, action.question or "", evt.id)
        if t == ActionType.propose_skill and self.workbench:
            evt = self._log(Actor.loop, "skill_proposed", {"target": current.id, "rung": 5, "draft_chars": len(json.dumps(action.skill_draft or {}))})
            outcome = await self.workbench.propose(self.store, current, action.skill_draft or {}, evt.id)
            self._log(Actor.loop, "skill_result", {"target": current.id, "saved": outcome.startswith("the verifier passed"), "result": outcome[:400]})
            return current, outcome
        return current, self._reject(action, f"{t.value} is not available in this phase")

    def _kept_skills(self, proto) -> str:
        """The bodies of the guidance skills a protocol step keeps in the view for all its turns."""
        lib = self.config.library
        if proto is None or lib is None or proto.terminal:
            return ""
        return "\n\n".join(lib.body_for(n)[:6000] for n in proto.current.skills)

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
                self._record_result(f"skill.{sk.key}", reg.run_skill_now(sk, ToolContext(store=self.store, target=target, event_id=evt.id), {}))  # run by code (rung 1): not a model-chosen tool, so it is not counted as "run" for a protocol step
        facts = {f.key: f.value for f in self.store.current_facts(target.id) if f.status != FactStatus.unknown}
        reused = False
        # a recipe skill only supplies facts at this rung (it ran above as an identify skill): it never answers the whole question, the deploy is still the model's to choose
        matches = [s for s in lib.match(target, facts, reg.platform) if s.meta.provenance == "generated" and s.meta.recipe is None]
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
        facts = self.store.current_facts(target.id)
        return EvidenceView({f.key: f for f in facts}, set(self.store.tools_run(target.id)), tags, proto.taken, sum(1 for f in facts if f.created_by == CreatedBy.human and f.status != FactStatus.unknown))

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

    def _progress(self, target: Target) -> tuple:
        """What counts as progress: a new fact, a protocol step moved, a tool that succeeded, a human answer."""
        proto = self._proto_runs.get(target.id)
        return (target.id, len(self.store.current_facts(target.id)), proto.state if proto else None, len(self.store.tools_run(target.id)), len(self.store.events(type="human_answer")))

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
        asked = (target.id, entry.name, json.dumps(action.args, sort_keys=True, default=str))
        if asked in self._asked_before and not entry.spec.repeatable:  # the same question again cannot get a different answer: its result is already in the facts
            self.summary.repeat_refusals += 1
            self._stuck[target.id] = self._stuck.get(target.id, 0) + 1
            self._log(Actor.loop, "repeat_refused", {"tool": entry.name, "target": target.id})
            return self._reject(action, f"{entry.name} was already asked with these arguments; its answer is in the facts")
        self._asked_before.add(asked)
        self._stuck[target.id] = 0

        verdict = decide(entry.spec, target, self.policy_state)
        self._log(Actor.policy, "policy_decision", {"tool": entry.name, "target": target.id,
                                                    "decision": verdict.decision.value, "reason": verdict.reason})
        if verdict.decision == Decision.deny:
            self.summary.policy_denied += 1
            return f"{entry.name} denied by policy: {verdict.reason}"
        if verdict.decision == Decision.needs_approval:
            t0 = time.monotonic()
            granted = bool(self._approve and self._approve(entry.spec, target))
            self._waited += time.monotonic() - t0
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
        if not result.ok and re.search(r"(^|: )HANG:", result.error or ""):  # the adapter words it "<reason>: HANG: ..."
            self.summary.hangs += 1
            self._log(Actor.loop, "hang", {"kind": "tool", "tool": entry.name, "detail": result.error[:160]})
        if not result.ok:
            self._asked_before.discard(asked)  # a failed attempt is not an answer: the model may try again (the no-progress guard bounds it)
        self._last_ok_tool = entry.name if result.ok else None
        return self._record_result(entry.name, result, target.id)

    def _record_result(self, tool: str, result: ProbeResult, target_id: str | None = None) -> str:
        if not result.ok:
            self._log(Actor.loop, "tool_result", {"tool": tool, "target": target_id, "ok": False, "error": result.error})
            return f"{tool} failed: {result.error}"
        from envrec.privacy.gate import PrivacyGate
        PrivacyGate(self.store, None, Salts(), tool).write_facts(result.facts)  # the one fact boundary
        for u in result.unknowns:
            self._unknown_reasons[u.key] = u.question or "could not be determined"
        unknown = sum(1 for f in result.facts if f.status == FactStatus.unknown)
        self._log(Actor.loop, "tool_result", {
            "tool": tool, "target": target_id, "ok": True, "facts": [f.key for f in result.facts], "unknown": unknown,
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
        # a fallback is the loop's own action (the model gave nothing valid): the fact is the loop's, not the model's
        self.store.write_fact(Fact(target_id=current.id, key=key, created_by=CreatedBy.loop if fallback else CreatedBy.model))
        self._unknown_reasons[key] = action.question or ("recorded by the loop: the model gave no valid action" if fallback else "recorded unknown by the model")
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
        if current.id in self._asked:  # the human was asked about this device already: asking again changes nothing
            return self._reject(action, "the human was already asked about this device")
        self._asked.add(current.id)
        evt = self._log(Actor.human, "human_question", {"key": key, "question": question, "target": current.id})
        t0 = time.monotonic()
        answer = self._ask_human(question) if self._ask_human else None
        self._waited += time.monotonic() - t0
        if not answer:
            self._blocked = True  # nobody can answer: the run ends cleanly, saying it needs a human
            self._log(Actor.human, "human_unavailable", {"key": key, "question": question[:200]})
            return "asked the human; no answer is available"
        self.store.write_fact(Fact(
            target_id=current.id, key=key, value=answer[:200], status=FactStatus.tested,
            created_by=CreatedBy.human,
            evidence=[Evidence(provider="human", probe="ask_human", event_id=evt.id, confidence=1.0,
                               limitations=["stated by the human, not measured"])]))
        self._log(Actor.human, "human_answer", {"key": key})
        return f"the human answered for {key}: " + quoted(answer, 800)  # what the owner said, quoted: it reaches the model on the next turn

    def _next_ring_target(self, current: Target, ring: int | None = None) -> Target | None:
        """The nearest unvisited target on a further ring that hangs off the current one."""
        seen = {v.id for v in self._visited}
        cands = [t for t in self.store.list_targets()
                 if t.id not in seen and t.ring > current.ring and t.parent in (None, current.id)
                 and (ring is None or t.ring == ring)]
        return min(cands, key=lambda t: t.ring, default=None)

    def _candidates(self, current: Target) -> list[Target]:
        """Targets the model may move to: unvisited, on a further ring, hanging off the current one (or off nothing)."""
        seen = {v.id for v in self._visited}
        return [t for t in self.store.list_targets() if t.id not in seen and t.ring > current.ring and t.parent in (None, current.id)]

    def _extend(self, action: Action, current: Target) -> tuple[Target, str]:
        wanted = action.ring
        if action.target_id:  # the model picked one of the candidates it was shown
            nxt = next((t for t in self._candidates(current) if t.id == action.target_id), None)
            if nxt is None:
                return current, self._reject(action, f"{action.target_id!r} is not one of the candidates")
        else:
            nxt = self._next_ring_target(current, wanted)
        if nxt is None:
            return current, self._reject(action, f"no target on ring {wanted} to move to")
        proto = self._proto(current)
        if proto is not None:
            proto.taken["extend_to"] = proto.taken.get("extend_to", 0) + 1
        self._log(Actor.loop, "ring_extended", {"from": current.id, "to": nxt.id, "ring": nxt.ring, "by": "model"})
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

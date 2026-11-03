"""The router (docs/ROUTER_API.md). `route` is pure over facts; `dispatch` hands a decision to the task desk; `report_result` re-enters a code grader's verdict and escalates at most twice.

Actors: a route decision and an escalation are ROUTER events (the written rule chose); refusals, dispatches and owner notices are GUARD events. Owner choices stay OWNER events and are not made here."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from envrec_coordinator.router import evidence as EV
from envrec_coordinator.router import grading, strings
from envrec_coordinator.router.catalog import build_candidates, knows_task_type
from envrec_coordinator.router.facts import Facts, Policy, admission_key
from envrec_coordinator.router.model import GradeResult, Refusal, Route, RouteDecision, Task, TaskHandle
from envrec_coordinator.router.outputs import MemoryOutputs, Outputs
from envrec_coordinator.router.rule import domain_step, exclusion_step, measured, run_rule, sensitivity_step
from envrec_coordinator.router.sensitivity import creation_refusal, filter_outbound, highest
from envrec_net import envrun as E

Log = Callable[[str, dict, str], None]
CODE_GRADERS = ("code", "result_verification")
RESULT_OF = "result-of:"
INSTRUCTION = "instr:"


class Router:
    def __init__(self, *, envs: dict[str, dict], facts: Facts, policy: Policy, log: Log | None = None, sender: Any = None, payloads: Callable[[str], str | None] | None = None,
                 outputs: Outputs | None = None, instructions: dict[str, str] | None = None, sandbox_ready: Callable[[], bool] | None = None, sandbox_runner: grading.SandboxRunner | None = None,
                 evidence: dict | None = None) -> None:
        self.envs, self.facts, self.policy = envs, facts, policy
        self.evidence = EV.load() if evidence is None else evidence  # public evidence as data; the router only reads it
        self.outputs: Outputs = outputs if outputs is not None else MemoryOutputs()
        self.sandbox_ready, self.sandbox_runner = sandbox_ready or (lambda: False), sandbox_runner
        self._graders: dict[str, dict] = {}
        self.instructions = E.load_instructions() if instructions is None else instructions  # stored instruction texts (`instr:verify`), resolved here, never from a model
        self.log: Log = log or (lambda t, p, a: None)
        self.sender, self.payloads = sender, payloads
        self._state: dict[str, dict] = {}  # task id -> {task, tried: [route keys], step, decision, closed}

    # ---- route -------------------------------------------------------------------------------------------------------------------------------------------------------------------
    def route(self, task: Task) -> RouteDecision | Refusal:
        st = self._state.get(task.task_id)
        if st is not None and st["decision"] is not None and not st["closed"]:
            return st["decision"]  # idempotent per (task, escalation step)
        eff, unknown = self._effective(task)
        self._state[task.task_id] = st = {"task": task, "tried": [], "step": 0, "decision": None, "closed": False, "effective": eff, "unknown_inputs": unknown}
        return self._decide(st)

    def explain(self, task: Task) -> RouteDecision | Refusal:
        """What `route` would decide right now, with nothing recorded: no state, no event, no dispatch. The same code path on a throwaway copy, so the explanation can never differ from the decision."""
        twin = Router(envs=self.envs, facts=self.facts, policy=self.policy, log=lambda t, p, a: None, outputs=self.outputs, instructions=self.instructions, evidence=self.evidence)
        twin._state = {k: v for k, v in self._state.items() if k != task.task_id}  # the levels of earlier tasks, for `result-of` inputs
        return twin.route(task)

    def _input_level(self, ref: str) -> tuple[str | None, bool]:
        """(level, known) of one input. A `result-of:<task>` input carries the level of its source task (what the router knows, else what the store recorded); an unknown source is treated as confidential."""
        if not ref.startswith(RESULT_OF):
            return None, True
        src = ref[len(RESULT_OF):]
        if src in self._state:
            return self._state[src]["effective"], True
        rec = self.outputs.of_task(src)
        return (rec.sensitivity, True) if rec is not None else ("confidential", False)

    def _effective(self, task: Task) -> tuple[str, list[str]]:
        """The level the task is routed under: the highest of its own and its inputs'. A confidential result can never reach a cloud verifier by being passed along."""
        levels, unknown = [task.sensitivity or ""], []
        for ref in [task.payload_ref, *task.input_refs]:
            lvl, known = self._input_level(ref)
            if lvl is not None:
                levels.append(lvl)
            if not known:
                unknown.append(ref)
        return (highest(*levels) if task.sensitivity in ("public", "internal", "confidential") else (task.sensitivity or "")), unknown

    def _log_refusal(self, r: Refusal) -> None:
        d = r.model_dump()
        self.log("route_refusal", {"task_id": d["task_id"], "refusal": d["code"], "reason": d["reason"], "details": d["details"]}, "GUARD")  # the event log refuses keys like "code"

    def _refuse(self, task: Task, code: str, **details: Any) -> Refusal:
        return self._refuse_id(task.task_id, code, **details)

    def _refuse_id(self, task_id: str, code: str, **details: Any) -> Refusal:
        r = Refusal(task_id=task_id, code=code, reason=strings.refusal_text(details.get("why", code)), details=details)  # `why` names a more exact typed reason for the same refusal code
        self._log_refusal(r)
        return r

    def _decide(self, st: dict) -> RouteDecision | Refusal:
        task: Task = st["task"]
        eff = st["effective"]
        bad = creation_refusal(task, eff)
        if bad is not None:
            self._log_refusal(bad)
            return bad
        if not knows_task_type(self.envs, task.task_type):
            return self._refuse(task, "no_candidate", why="unknown_task_type", candidates=[], environments={})  # no environment declares this type: a refusal with zero candidates, nothing invented
        detail: dict[str, dict] = {}
        cands, left_out = build_candidates(task, self.envs, self.facts, self.policy, detail)
        reasons = {k: strings.reason_text(v, detail.get(k)) for k, v in left_out.items()}
        cand_keys = [c.route.key for c in cands]  # every refusal below carries them: the reasoning is candidates, then who was cut and why
        if not cands:
            present = set(left_out.values())  # the most actionable reason wins; the details still list every environment with its own
            code = next((c for c in ("machine_offline", "sandbox_missing", "automated_use_unreviewed") if c in present), "no_candidate")  # sandbox_missing: an environment got past every other check
            return self._refuse(task, code, environments=reasons, codes=left_out, candidates=cand_keys)
        kept, dropped, first = sensitivity_step(cands, eff, self.facts, task.task_type, self.policy, set(st["tried"]))
        if st["unknown_inputs"]:
            first.note += f"; inputs of unknown source treated as confidential: {st['unknown_inputs']}"
        if eff != task.sensitivity:
            first.note += f"; raised from {task.sensitivity} by an input"
        pre_exclusion, ex_trace = kept, []
        if kept and task.exclude is not None:
            kept, ex_dropped, ex_step = exclusion_step(kept, task.exclude)
            ex_trace = [ex_step]
            if not kept:  # the exclusion is a hard filter: it is never relaxed, and the refusal is the normal one
                if st["step"] > 0:
                    return self._exhausted(st, left_out, {**dropped, **ex_dropped})
                code = "no_local_candidate" if eff == "confidential" else "no_candidate"
                return self._refuse(task, code, environments=reasons, dropped={k: strings.REASONS.get(v, v) for k, v in {**dropped, **ex_dropped}.items()}, excluded=sorted(ex_dropped), candidates=cand_keys)
        if not kept:
            if st["step"] > 0:
                return self._exhausted(st, left_out, dropped)
            if eff == "confidential":
                code = "provisional_only" if "provisional" in dropped.values() else "no_local_candidate"
            else:
                code = "no_candidate"
            return self._refuse(task, code, environments=reasons, dropped={k: strings.REASONS.get(v, v) for k, v in dropped.items()}, candidates=cand_keys)
        dom_trace = []
        if task.domain:
            kept, dom_step = domain_step(kept, task.domain)
            dom_trace = [dom_step]
        tiers = self._tiers(kept, task)
        chosen, trace, label = run_rule(kept, self.facts, task.task_type, self.policy, tiers)
        if chosen is None:
            if st["step"] > 0:
                return self._exhausted(st, left_out, dropped)
            return self._refuse(task, "no_candidate", environments=reasons, trace=[s.model_dump() for s in trace], candidates=cand_keys)
        decision = RouteDecision(task_id=task.task_id, route=chosen.route, rule_trace=[first, *ex_trace, *dom_trace, *trace], escalation_step=st["step"], evidence_label=label, provisional=label == strings.EVIDENCE_NONE, left_out=dict(reasons), effective_sensitivity=eff,
                                 evidence=self._evidence_of(chosen, task))  # type: ignore[arg-type]
        st["decision"] = decision
        st["tried"].append(chosen.route.key)
        self.log("route_decision", {**decision.model_dump(), "sensitivity": eff, "entry_chat": task.entry_chat.model_dump(), "left_out": left_out}, "ROUTER")
        return decision

    def _evidence_of(self, c, task: Task) -> dict:
        """Tier, source and date of what is known about this combination for this task (router/evidence.py): a valid measurement first, then public data, else none."""
        m = measured(c, self.facts, task.task_type, self.policy)
        fact = self.facts.admission(admission_key(task.task_type, c.route)) if isinstance(m, tuple) else None
        return EV.describe(c.route, task.task_type, task.domain, fact, m if isinstance(m, tuple) else None, self.evidence)

    def _tiers(self, cands, task: Task) -> dict[str, int] | None:
        """Evidence rank of each candidate, only when the data holds public evidence for at least one of them; otherwise None and the rule runs exactly as before."""
        if not self.evidence.get("entries"):
            return None
        rank = {c.route.key: EV.RANK[self._evidence_of(c, task)["tier"]] for c in cands}
        return rank if any(r in (1, 2) for r in rank.values()) else None

    # ---- escalation --------------------------------------------------------------------------------------------------------------------------------------------------------------
    def report_result(self, task_id: str, escalation_step: int, grader_result: dict) -> RouteDecision | Refusal | None:
        """None: the task passed and is closed. Otherwise the next decision, or Refusal(escalation_exhausted) after at most `max_escalation` steps up."""
        st = self._state.get(task_id)
        if st is None or st["closed"] or st["decision"] is None or st["decision"].escalation_step != escalation_step:
            return Refusal(task_id=task_id, code="stale_decision", reason=strings.refusal_text("stale_decision"))
        task: Task = st["task"]
        if grader_result.get("grader") not in CODE_GRADERS:
            return self._refuse(task, "weak_grader", grader=grader_result.get("grader"))  # a model-graded result can neither pass nor fail a task
        if grader_result.get("passed"):
            st["closed"] = True
            return None
        if st["step"] >= self.policy.max_escalation:
            return self._exhausted(st, {}, {})
        frm = st["decision"]
        st["step"] += 1
        nxt = self._decide(st)
        if isinstance(nxt, RouteDecision):
            self.log("escalation", {"task_id": task_id, "from": frm.route.key, "to": nxt.route.key, "step": st["step"], "grader": grader_result.get("grader"), "evidence_ref": grader_result.get("evidence_ref")}, "ROUTER")
            return nxt
        return nxt

    def _exhausted(self, st: dict, left_out: dict, dropped: dict) -> Refusal:
        task: Task = st["task"]
        st["closed"] = True
        notice = filter_outbound({"kind": "text", "actor": "GUARD", "sensitivity": st["effective"], "payload": {"task_id": task.task_id, "state": "needs you", "machine": ""},
                                  "text_fallback": strings.OWNER_NOTICE.format(task_id=task.task_id, tried=len(st["tried"]))}, task.entry_chat.model_dump(), outbound_policy=self.policy.outbound)
        self.log("owner_notice", notice, "GUARD")
        return self._refuse(task, "escalation_exhausted", tried=list(st["tried"]), owner_notice=notice, environments=left_out)

    # ---- dispatch ----------------------------------------------------------------------------------------------------------------------------------------------------------------
    def dispatch(self, decision: RouteDecision) -> TaskHandle | Refusal:
        st = self._state.get(decision.task_id)
        if st is None or st["decision"] is not decision and st["decision"] != decision:
            return Refusal(task_id=decision.task_id, code="stale_decision", reason=strings.refusal_text("stale_decision"))
        task: Task = st["task"]
        if self.sender is None:
            return self._refuse(task, "no_sender")
        entry = self.envs[decision.route.environment_id]
        machine = next((m for m in self.facts.machines() if m.machine_id == decision.route.machine_id), None)
        mode = E.mode_of(entry, task.task_type, decision.route.access_id)
        if machine is None or mode is None or not machine.paired:
            return self._refuse(task, "no_candidate", machine=decision.route.machine_id)
        if not machine.online:
            return self._refuse(task, "machine_offline", machine=machine.machine_id)
        if mode["confinement"] in E.NEEDS_SANDBOX and not machine.sandbox_backends:
            return self._refuse(task, "sandbox_missing", environment=entry["id"], machine=machine.machine_id)
        text, level = self._read_inputs(task, decision.effective_sensitivity)
        if text is None:
            return self._refuse(task, "payload_missing", payload_ref=task.payload_ref)
        return self._send(task, decision.route, decision.escalation_step, text, level, mode)

    def run_prompt(self, task: Task, route: Route, text: str) -> TaskHandle | Refusal:
        """Send one prompt to exactly this combination, for a measurement (the admission exam). It is not a route decision: nothing is chosen, nothing escalates. The same guards as `dispatch`."""
        if self.sender is None:
            return self._refuse(task, "no_sender")
        entry = self.envs.get(route.environment_id)
        machine = next((m for m in self.facts.machines() if m.machine_id == route.machine_id), None)
        mode = E.mode_of(entry, task.task_type, route.access_id) if entry is not None else None
        if entry is None or machine is None or mode is None or not machine.paired:
            return self._refuse(task, "no_candidate", machine=route.machine_id)
        if not machine.online:
            return self._refuse(task, "machine_offline", machine=route.machine_id)
        if mode["confinement"] in E.NEEDS_SANDBOX and not machine.sandbox_backends:
            return self._refuse(task, "sandbox_missing", environment=entry["id"], machine=machine.machine_id)
        return self._send(task, route, 0, text, task.sensitivity or "public", mode)

    def _send(self, task: Task, route: Route, step: int, text: str, level: str, mode: dict) -> TaskHandle | Refusal:
        entry = self.envs[route.environment_id]
        machine = next(m for m in self.facts.machines() if m.machine_id == route.machine_id)
        if level == "confidential" and E.is_cloud(entry, route.access_id):  # a confidential result can never be handed to a cloud environment, whatever path it took to get here
            return self._refuse(task, "confidential_cloud_environment", environment=entry["id"])
        local = E.uses_local_endpoint(entry, route.access_id)  # the node's loopback model server; an API access names its own address in the data
        params = {"environment_id": entry["id"], "task_type": task.task_type, "sensitivity": level, "mode": mode["name"], "workspace_id": re.sub(r"[^a-z0-9-]", "-", f"{task.task_id}-{step}".lower())[:48].strip("-") or "task",
                  "recipe_sha256": E.recipe_sha256(entry), "access_id": route.access_id, "input_text": text, "endpoint": machine.model_endpoint if local else None,
                  "model_id": (route.model_id if E.listed_model_ok(entry, route.access_id, route.model_id) else None) or E.declared_model(entry, route.access_id) or (machine.model_id if local else None)}
        rec = self.sender.start_environment_run(machine.machine_id, task.task_id, entry, params, {"task_id": task.task_id, "step": step, "route": route.key})
        self.log("dispatch", {"task_id": task.task_id, "route": route.key, "node_task": rec.id, "state": rec.status, "reason": rec.reason, "workspace": params["workspace_id"]}, "GUARD")
        if rec.status == "rejected":
            code = rec.reason if rec.reason in strings.REFUSALS else "dispatch_rejected"
            return self._refuse(task, code, node_reason=rec.reason, machine=machine.machine_id)
        return TaskHandle(task_id=task.task_id, machine_id=machine.machine_id, node_task_id=rec.id, task_state=rec.status, escalation_step=step)

    def _read_inputs(self, task: Task, level: str) -> tuple[str | None, str]:
        """The task's input text (its reference, then the further inputs) and the level it is sent under: the highest of the decision's and the stored level of every `result-of` input. None when one is missing."""
        parts = []
        for i, ref in enumerate([task.payload_ref, *task.input_refs]):
            if ref.startswith(RESULT_OF):
                rec = self.outputs.of_task(ref[len(RESULT_OF):])
                if rec is None:
                    return None, level
                level, text = highest(level, rec.sensitivity), rec.text
            elif ref.startswith(INSTRUCTION):
                text = self.instructions.get(ref)  # project-written text with no task data: it never raises the level
                if text is None:
                    return None, level
            else:
                text = self.payloads(ref) if self.payloads else None
                if text is None:
                    return None, level
            parts.append(text if i == 0 else f"\n\n--- input {i + 1} ---\n\n{text}")
        return "".join(parts), level

    # ---- results and grading (section 10) ------------------------------------------------------------------------------------------------------------------------------
    def result_of(self, handle: TaskHandle) -> dict:
        """{state, output_ref}: the node task's state, and, once it is `done`, a CaptureRef of its output stored with the level the task was routed under."""
        rec = getattr(self.sender, "tasks", {}).get(handle.node_task_id)
        if rec is None:
            return {"state": "unknown", "output_ref": None}
        ref = None
        if rec.status == "done":
            ref = self.outputs.put(handle.task_id, self._state[handle.task_id]["effective"], rec.result)
        return {"state": rec.status, "output_ref": ref}

    def set_grader(self, task_id: str, spec: dict) -> None:
        """Register the code grader of a task (by whoever created it). The kinds are those of `grading`; anything else is refused when it is applied."""
        self._graders[task_id] = dict(spec)

    def grade(self, task_id: str, output_ref: str) -> GradeResult | Refusal:
        spec = self._graders.get(task_id)
        if spec is None:
            return self._refuse_id(task_id, "no_grader")
        rec = self.outputs.get(output_ref)
        if rec is None or rec.task_id != task_id:
            return self._refuse_id(task_id, "output_missing", output_ref=output_ref)
        kind = spec.get("kind")
        extra = ""
        if kind in grading.WEAK_KINDS:
            return self._refuse_id(task_id, "weak_grader", grader=kind)  # a model or a person grading does not pass a task
        if kind in grading.EXECUTING_KINDS:
            if not self.sandbox_ready() or self.sandbox_runner is None:
                return self._refuse_id(task_id, "sandbox_missing", grader=kind)  # model-written code is never executed outside our sandbox
            try:
                out = self.sandbox_runner(rec, spec)
            except grading.SandboxGone:
                return self._refuse_id(task_id, "sandbox_missing", grader=kind)
            passed, extra = bool(out.get("passed")), str(out.get("log_sha256", ""))
        elif kind in grading.TEXT_KINDS:
            try:
                passed = grading.check_text(spec, rec.text)
            except (KeyError, ValueError, re.error):
                return self._refuse_id(task_id, "no_grader", grader=kind)
        else:
            return self._refuse_id(task_id, "no_grader", grader=kind)
        ev = grading.evidence_ref(task_id, output_ref, spec, passed, extra)
        self.log("grade", {"task_id": task_id, "output_ref": output_ref, "grader": kind, "passed": passed, "evidence_ref": ev}, "GUARD")
        return GradeResult(passed=passed, grader="code", evidence_ref=ev)

    def filter_outbound(self, intent: dict, entry_chat: dict | None) -> dict:
        return filter_outbound(intent, entry_chat, outbound_policy=self.policy.outbound)

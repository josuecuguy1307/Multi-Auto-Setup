"""The router (docs/ROUTER_API.md). `route` is pure over facts; `dispatch` hands a decision to the task desk; `report_result` re-enters a code grader's verdict and escalates at most twice.

Actors: a route decision and an escalation are ROUTER events (the written rule chose); refusals, dispatches and owner notices are GUARD events. Owner choices stay OWNER events and are not made here."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from envrec_coordinator.router import grading, strings
from envrec_coordinator.router.catalog import build_candidates, knows_task_type
from envrec_coordinator.router.facts import Facts, Policy
from envrec_coordinator.router.model import GradeResult, Refusal, RouteDecision, Task, TaskHandle
from envrec_coordinator.router.outputs import MemoryOutputs, Outputs
from envrec_coordinator.router.rule import exclusion_step, run_rule, sensitivity_step
from envrec_coordinator.router.sensitivity import creation_refusal, filter_outbound, highest
from envrec_net import envrun as E

Log = Callable[[str, dict, str], None]
CODE_GRADERS = ("code", "result_verification")
RESULT_OF = "result-of:"
INSTRUCTION = "instr:"


class Router:
    def __init__(self, *, envs: dict[str, dict], facts: Facts, policy: Policy, log: Log | None = None, sender: Any = None, payloads: Callable[[str], str | None] | None = None,
                 outputs: Outputs | None = None, instructions: dict[str, str] | None = None, sandbox_ready: Callable[[], bool] | None = None, sandbox_runner: grading.SandboxRunner | None = None) -> None:
        self.envs, self.facts, self.policy = envs, facts, policy
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
        r = Refusal(task_id=task_id, code=code, reason=strings.refusal_text(code), details=details)
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
            return self._refuse(task, "unknown_task_type")
        cands, left_out = build_candidates(task, self.envs, self.facts, self.policy)
        reasons = {k: strings.REASONS.get(v, v) for k, v in left_out.items()}
        if not cands:
            present = set(left_out.values())  # the most actionable reason wins; the details still list every environment with its own
            code = next((c for c in ("sandbox_missing", "automated_use_unreviewed") if c in present), "no_candidate")  # sandbox_missing: an environment got past every other check
            return self._refuse(task, code, environments=reasons, codes=left_out)
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
                return self._refuse(task, code, environments=reasons, dropped={k: strings.REASONS.get(v, v) for k, v in {**dropped, **ex_dropped}.items()}, excluded=sorted(ex_dropped))
        if not kept:
            if st["step"] > 0:
                return self._exhausted(st, left_out, dropped)
            if eff == "confidential":
                code = "provisional_only" if "provisional" in dropped.values() else "no_local_candidate"
            else:
                code = "no_candidate"
            return self._refuse(task, code, environments=reasons, dropped={k: strings.REASONS.get(v, v) for k, v in dropped.items()})
        chosen, trace, label = run_rule(kept, self.facts, task.task_type, self.policy)
        if chosen is None:
            if st["step"] > 0:
                return self._exhausted(st, left_out, dropped)
            return self._refuse(task, "no_candidate", environments=reasons, trace=[s.model_dump() for s in trace])
        decision = RouteDecision(task_id=task.task_id, route=chosen.route, rule_trace=[first, *ex_trace, *trace], escalation_step=st["step"], evidence_label=label, effective_sensitivity=eff)  # type: ignore[arg-type]
        st["decision"] = decision
        st["tried"].append(chosen.route.key)
        self.log("route_decision", {**decision.model_dump(), "sensitivity": eff, "entry_chat": task.entry_chat.model_dump(), "left_out": left_out}, "ROUTER")
        return decision

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
        mode = E.mode_of(entry, task.task_type)
        if machine is None or mode is None or not machine.paired:
            return self._refuse(task, "no_candidate", machine=decision.route.machine_id)
        if mode["confinement"] in E.NEEDS_SANDBOX and not machine.sandbox_backends:
            return self._refuse(task, "sandbox_missing", environment=entry["id"], machine=machine.machine_id)
        text, level = self._read_inputs(task, decision.effective_sensitivity)
        if text is None:
            return self._refuse(task, "payload_missing", payload_ref=task.payload_ref)
        if level == "confidential" and entry.get("cloud"):  # a confidential result can never be handed to a cloud environment, whatever path it took to get here
            return self._refuse(task, "confidential_cloud_environment", environment=entry["id"])
        needs_model = entry["subtype"] in ("local_model", "own_harness")
        params = {"environment_id": entry["id"], "task_type": task.task_type, "sensitivity": level, "mode": mode["name"], "workspace_id": re.sub(r"[^a-z0-9-]", "-", f"{task.task_id}-{decision.escalation_step}".lower())[:48].strip("-") or "task",
                  "recipe_sha256": E.recipe_sha256(entry), "input_text": text, "endpoint": machine.model_endpoint if needs_model else None, "model_id": machine.model_id if needs_model else None}
        rec = self.sender.start_environment_run(machine.machine_id, task.task_id, entry, params, {"task_id": task.task_id, "step": decision.escalation_step, "route": decision.route.key})
        self.log("dispatch", {"task_id": task.task_id, "route": decision.route.key, "node_task": rec.id, "state": rec.status, "reason": rec.reason, "workspace": params["workspace_id"]}, "GUARD")
        if rec.status == "rejected":
            code = rec.reason if rec.reason in strings.REFUSALS else "dispatch_rejected"
            return self._refuse(task, code, node_reason=rec.reason, machine=machine.machine_id)
        return TaskHandle(task_id=task.task_id, machine_id=machine.machine_id, node_task_id=rec.id, task_state=rec.status, escalation_step=decision.escalation_step)

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

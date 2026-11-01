"""The director-with-workers pattern on the board, against the router API (docs/ROUTER_API.md).

* The director is a model that picks one validated action per turn (`director.ModelTools`). It never picks routes, machines or models.
* Each subtask goes to the router (`route`). The decision, or the refusal, is recorded on the board. The router, not this module, decides where it runs.
* Workers are the machines the router named: they take the subtask with a lease, the router dispatches it, a code grader judges the output, and `report_result` re-enters the verdict.
  A failed code grade may produce an escalation; by default every escalation asks the owner (a pending decision answered only by a human gesture).
* Results are verified by a code grader or by a verification task on a different machine; a model-written result is a hypothesis, never tested.
* This module advances mechanical work (route, claim, dispatch, grade, report). It never chooses the director's action, never accepts a result for it and never declares the run finished: only the
  director's `finish` or `escalate_to_owner` closes the protocol.

Topology and freedom come from the owner's draft (T3b) as data: `TeamPolicy.from_draft`. SEAM: until T3b merges the conservative default is used (director on the coordinator, escalations within a task run up to the router's maximum of 2 and each one is announced to the owner).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

from envrec_board import strings
from envrec_board.board import Board
from envrec_board.decisions import DecisionDesk
from envrec_board.director import MAX_SUBTASKS, VERIFY_GRADER, DirectorView, ModelTools
from envrec_board.plan import PlanBook
from envrec_board.router_port import FATAL_REFUSALS, GRADER_KINDS, RouterPort, is_refusal
from envrec_board.vocab import BoardError

DEFAULT_DRAFT = {"director_machine": "coordinator", "worker_machines": "paired", "preapproved_escalations": 2}


@dataclass
class TeamPolicy:
    director_machine: str = "coordinator"
    worker_machines: str | list[str] = "paired"  # who may work is the router's call; this is the owner's stated topology, kept as data
    preapproved_escalations: int = 2  # escalation steps that run without a question (the router's maximum); the owner is told of each one. 0 = every escalation asks. The draft's freedom dial tunes this later
    max_turns: int = 40

    @classmethod
    def from_draft(cls, draft: dict | None) -> "TeamPolicy":
        d = {**DEFAULT_DRAFT, **(draft or {})}
        return cls(d["director_machine"], d["worker_machines"], int(d["preapproved_escalations"]))


def render_view(board: Board, root_id: str, view: DirectorView) -> str:
    """What the director reads each turn. Built from the board and the menu spec: what is known, what the router refused and why, and the actions offered now. No routes, no candidates."""
    root = board.tasks[root_id]
    lines = [f"Owner task {root_id} ({root['sensitivity']}): {root['input']}", "Subtasks:"]
    subs = view.subtasks()
    if not subs:
        lines.append("- none yet")
    for s in subs:
        bits = [strings.STATE_TEXT[s["status"]]]
        if s["route_refusal"]:
            bits.append(f"the router refused it [{s['route_refusal']['code']}]: {s['route_refusal']['reason']}")
        elif s["approval"] != "approved":
            bits.append({"unapproved": strings.UNAPPROVED, "pending": strings.PLAN_WITH_OWNER, "declined": strings.DECLINED}[s["approval"]])
        elif s["route"] is None:
            bits.append("waiting for the router")
        if s["result"]:
            bits.append(f"result is {s['result']['evidence_status']}")
        for v in s["verification"]:
            bits.append(f"verification {v['task_id']}: {strings.STATE_TEXT[v['status']]}")
        if s["director_decision"]:
            bits.append(s["director_decision"])
        lines.append(f"- {s['task_id']} ({s['task_type']}): " + "; ".join(bits))
    lines.append("Actions you may take now (pick exactly one):")
    for name, spec in view.menu().items():
        args = ", ".join(k for k in spec if k not in ("subtask_ids", "tasks_waiting"))
        on = f" [subtask_id one of {', '.join(spec['subtask_ids'])}]" if "subtask_ids" in spec else (f" [for {', '.join(spec['tasks_waiting'])}]" if "tasks_waiting" in spec else "")
        lines.append(f"- {name}({args}){on}")
    return "\n".join(lines)


def parse_grader(test_ref: str) -> dict | None:
    """`kind:value` for a code grader kind of ROUTER_API section 10. Anything else (a model's opinion, a missing grader) sets none, and the result stays a hypothesis."""
    kind, _, value = (test_ref or "").partition(":")
    return {"kind": kind, "value": value} if kind in GRADER_KINDS and value else None


class TeamRun:
    def __init__(self, board: Board, desk: DecisionDesk, router: RouterPort, *, root_id: str, entry_chat: dict, policy: TeamPolicy | None = None) -> None:
        self.board, self.desk, self.router = board, desk, router
        self.root_id, self.entry_chat, self.policy = root_id, entry_chat, policy or TeamPolicy()
        self.book = PlanBook(board, root_id)
        self.tools = ModelTools(board, root_id, desk=desk, on_submit_plan=self.submit_plan)
        desk.hooks["decompose"] = self._plan_approved
        self.stop: str | None = None

    # ---- the owner's plan: exactly the listed subtasks, each with one verification slot ----------------------------------------------------------

    def _router_type(self, t: dict) -> str:
        return self.board.tasks[t["verifies"]]["task_type"] if t["verifies"] else t["task_type"]  # a verification is work of the type it verifies

    def submit_plan(self) -> dict | None:
        """The director asks the owner to approve every task that has no approval yet, plus one verification slot for each new subtask. One pending decision, bound to the exact list."""
        ids = self.book.unapproved(self.desk)
        if not ids or self.book.pending_plan(self.desk):
            return None
        items = []
        for tid in ids:
            t = self.board.tasks[tid]
            items.append({"task_id": tid, "subtask_id": tid, "kind": "verification" if t["verifies"] else "subtask", "task_type": self._router_type(t), "sensitivity": t["sensitivity"],
                          "input_ref": t["input"], "test_ref": t["test"], "for": t["verifies"]})
            if not t["verifies"]:  # the slot's id is its own, so the router matches the verification to exactly this subtask's slot
                items.append({"task_id": None, "subtask_id": f"verify-of:{tid}", "kind": "verification_slot", "task_type": t["task_type"], "sensitivity": t["sensitivity"], "input_ref": f"result-of:{tid}",
                              "test_ref": None, "for": tid})
        return self.desk.ask("decompose", self.root_id, strings.plan_summary(self.root_id, items), {"items": items})

    def _plan_approved(self, ask, answer) -> None:
        """Runs inside the owner's human-gesture answer: the router writes its OWNER event for exactly this list, and the board records the plan."""
        items = ask.payload["details"]["items"]
        subs = [{"id": i["subtask_id"], "type": i["task_type"], "sensitivity": i["sensitivity"]} for i in items]
        ev = self.router.record_owner_decision(self.root_id, subs, answer.payload["channel"])  # the gesture that was just made
        dec = self.router.approve_decomposition(self.root_id, subs, ev)  # the router re-checks that event in its own store
        if is_refusal(dec):
            self.board.note("plan_refused", self.root_id, {"refusal": dec["code"], "reason": dec["reason"], "decision_id": ask.payload["decision_id"]})
            return
        self.board.note("plan_approved", self.root_id, {"decision_id": ask.payload["decision_id"], "decomposition_id": dec, "owner_event_id": ev, "items": items})

    # ---- mechanical work ----------------------------------------------------------------------------------------------------------------------

    def pump(self) -> dict:
        """Do every step that needs no model: register approved tasks, route them, run routed ones, handle escalations. Returns {progressed, waiting_owner}. Idempotent."""
        progressed, waiting = False, []
        if (did := self.book.pending_plan(self.desk)):
            waiting.append(did)
        for _ in range(100):
            moved = False
            for t in self.book.family():
                if self.stop:
                    break
                moved |= self._advance(t["task_id"], waiting)
            progressed |= moved
            if not moved or self.stop:
                break
        return {"progressed": progressed, "waiting_owner": sorted(set(waiting))}

    def _advance(self, tid: str, waiting: list) -> bool:
        t = self.board.tasks[tid]
        if t["status"] in ("failed", "unverified") and t["proposal"]:
            return self._escalation_answer(tid, waiting)
        if t["status"] != "open" or t["route_refusal"]:
            return False
        if t["route"] is None:
            return self._route(tid)
        return self._run(tid)

    def _origin(self, tid: str) -> bool:
        """Register the task with the router on the strength of the owner's plan. False when no plan covers it (or the router has no free slot for it)."""
        b = self.board
        if self.book.has_origin(tid):
            return True
        cl = self.book.claim_for(tid)
        if cl is None:
            return False
        plan, item = cl
        ev = self.router.own_subtask(tid, plan["decomposition_id"], item["subtask_id"])
        if ev is None:
            return False
        b.note("origin", tid, {"decomposition_id": plan["decomposition_id"], "origin_event": ev, "created_by": "PLANNER_ACCEPTED", **({"slot_for": item["for"]} if item["kind"] == "verification_slot" else {})})
        t = b.tasks[tid]
        spec = VERIFY_GRADER if t["verifies"] else parse_grader(t["test"])
        if spec:
            self.router.set_grader(tid, spec)
        return True

    def _route(self, tid: str) -> bool:
        b, t = self.board, self.board.tasks[tid]
        if not self._origin(tid):
            return False
        task = {"task_id": tid, "task_type": self._router_type(t), "sensitivity": t["sensitivity"], "entry_chat": self.entry_chat, "created_by": "PLANNER_ACCEPTED", "payload_ref": t["input"]}
        if t["input_refs"]:
            task["input_refs"] = list(t["input_refs"])
        if t["exclude"]:
            task["exclude"] = t["exclude"]
        r = self.router.route(task)
        if is_refusal(r):
            b.record_route_refusal(tid, r)
            if r["code"] in FATAL_REFUSALS:
                self._close("refused")
        else:
            b.record_route_decision(tid, r)
        return True

    def _run(self, tid: str) -> bool:
        b, t = self.board, self.board.tasks[tid]
        machine, decision = b.machine_of(tid), t["route"]
        try:
            lease = b.claim(tid, machine, actor="GUARD")
        except BoardError as e:  # the second guard: even if the router ignored `exclude`, the board refuses a verifier that is the machine that did the work
            b.record_route_refusal(tid, {"task_id": tid, "code": e.code, "reason": e.text, "details": {"machine_id": machine}})
            return True
        h = self.router.dispatch(decision)
        if is_refusal(h):
            b.note("dispatch_refused", tid, {"refusal": h["code"], "reason": h["reason"], "machine_id": machine})
            b.release(lease, "dispatch_rejected", actor="GUARD")
            b.record_route_refusal(tid, h)
            return True
        b.note("dispatched", tid, {"machine_id": machine, "handle": h.get("decision_ref")})
        got = self.router.result_of(h)
        if got["state"] != "done":
            b.release(lease, f"node_{got['state']}", actor="GUARD")
            return True
        g = self.router.grade(tid, got["output_ref"])
        if is_refusal(g):  # no code grade possible (no grader, sandbox missing, ...): the result is a hypothesis and nothing is reported to the router
            b.note("grade_refused", tid, {"refusal": g["code"], "reason": g["reason"]})
            b.result(lease, got["output_ref"], [], None, actor="GUARD")
            return True
        b.result(lease, got["output_ref"], [g["evidence_ref"]], {"passed": g["passed"], "by": "code"}, actor="GUARD")
        self._report(tid, g)
        return True

    def _report(self, tid: str, g: dict) -> None:
        b, t = self.board, self.board.tasks[tid]
        r = self.router.report_result(tid, t["escalation_step"], {"passed": g["passed"], "grader": g["grader"], "evidence_ref": g["evidence_ref"]})
        if r is None:
            return
        if is_refusal(r):
            b.record_route_refusal(tid, r)
            return
        step = r["escalation_step"]
        if step <= self.policy.preapproved_escalations:  # the owner's draft allowed this step without asking
            b.record_route_decision(tid, r)
            return
        route = r["route"]
        d = self.desk.ask("escalate_beyond_preapproved", tid, strings.escalation_summary(tid, b.machine_of(tid), route["environment_id"], route["machine_id"], step, r["evidence_label"]),
                          {"task_id": tid, "step": step, "machine_id": route["machine_id"], "environment_id": route["environment_id"]})
        b.propose_escalation(tid, r, d["decision_id"])

    def _escalation_answer(self, tid: str, waiting: list) -> bool:
        b, t = self.board, self.board.tasks[tid]
        p = t["proposal"]
        a = self.desk.answer_of(p["decision_id"])
        if a is None:
            waiting.append(p["decision_id"])
            return False
        if a.payload["option"] == "approve":
            route = p["decision"]["route"]
            self.desk.require(p["decision_id"], "escalate_beyond_preapproved", tid,
                              {"task_id": tid, "step": p["decision"]["escalation_step"], "machine_id": route["machine_id"], "environment_id": route["environment_id"]})
            b.record_route_decision(tid, p["decision"])
        else:
            b.decline_escalation(tid, p["decision_id"])
        return True

    # ---- the director's turn and the whole run ------------------------------------------------------------------------------------------------

    def director_turn(self, director: Callable[[str], Any]) -> dict:
        """One turn: the director reads the view and returns one action; the guard validates and applies it."""
        return self.tools.act(director(render_view(self.board, self.root_id, self.tools.view)))

    def _close(self, stop: str) -> None:
        if not self.stop:
            self.stop = stop
            self.board.note("run_stopped", self.root_id, {"stop": stop})

    def run(self, director: Callable[[str], Any]) -> dict:
        """Run until the protocol closes, the owner is needed, or the turn cap. Safe to call again after the owner answers."""
        for _ in range(self.policy.max_turns):
            info = self.pump()
            if self.stop:
                break
            if info["waiting_owner"]:  # the owner is needed before the director may act again
                return {"state": "waiting_for_owner", "decisions": info["waiting_owner"]}
            self.director_turn(director)
            if self.tools.stop:
                self.pump()
                self._close(self.tools.stop)
                break
        else:
            self._close("turn_cap")
        return {"state": "stopped", "stop": self.stop}

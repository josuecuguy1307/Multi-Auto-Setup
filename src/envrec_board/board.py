"""The typed board: tasks, leases, progress, results, node cards and published skills (docs/NETWORK_CONTRACT.md section 5).

Rules the code enforces (GUARD): a task has at most one live lease (no double claims); a lease has a deadline and an expired lease frees the task with a typed reason; a task cannot be taken without a
recorded router decision that names the machine taking it; a verification task is never taken by the machine that did the work; a derived item never lowers the sensitivity of its inputs; a node card
lists only capabilities with evidence and a date; a payload key that could carry a secret is refused.

The board never routes. `record_route_decision` stores a `RouteDecision` exactly as docs/ROUTER_API.md defines it; the board only checks that a decision exists and that the
claimer is the machine it names. Every state change is ONE appended entry, and the state is always the fold of the entries: `Board.replay(entries)` rebuilds the same state without a clock.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any, Callable

from envrec_board.log import BoardLog, MemoryLog
from envrec_board.vocab import ACTORS, RANK, REQUIRED, SENSITIVITY, BoardError, Entry, check_payload_keys, highest

Listener = Callable[[Entry, "Board"], None]
TERMINAL = ("done", "failed", "unverified", "exhausted")


def canonical_hash(obj: Any) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class Board:
    def __init__(self, log: BoardLog | None = None, clock: Callable[[], float] = time.time, *, max_attempts: int = 3, default_lease_s: float = 120.0, max_lease_s: float = 900.0,
                 max_extend_s: float = 300.0) -> None:
        self.log: BoardLog = log if log is not None else MemoryLog()
        self.clock = clock
        self.max_attempts, self.default_lease_s, self.max_lease_s, self.max_extend_s = max_attempts, default_lease_s, max_lease_s, max_extend_s  # coordinator settings (5.3, 5.4)
        self.listeners: list[Listener] = []
        self._lock = threading.RLock()
        self.tasks: dict[str, dict] = {}
        self.leases: dict[str, dict] = {}
        self.cards: dict[str, dict] = {}
        self.skills: dict[str, dict] = {}
        self.refusals = 0
        for e in self.log.entries():  # opening an existing log folds it: the state is never kept apart from the entries
            self._apply(e)

    # ---- replay ---------------------------------------------------------------------------------------------------------------------------

    @classmethod
    def replay(cls, entries: list[Entry], **kw) -> "Board":
        """The state the entries describe. Reads no clock and writes nothing."""
        b = cls(MemoryLog(), **kw)
        for e in entries:
            b.log._items.append(e)  # type: ignore[attr-defined]  # same entries, same numbers
            b._apply(e)
        return b

    def snapshot(self) -> dict:
        return {"tasks": self.tasks, "leases": self.leases, "cards": self.cards, "skills": self.skills}

    # ---- append + apply (one code path for live and replay) ----------------------------------------------------------------------------------

    def _emit(self, type: str, *, tag: str, actor: str, task_id: str | None, task_type: str | None, sensitivity: str, payload: dict) -> Entry:
        if tag not in ACTORS or actor not in ACTORS:
            raise BoardError("bad_entry", f"unknown actor {actor!r}/{tag!r}")
        if sensitivity not in SENSITIVITY:
            raise BoardError("bad_entry", f"unknown sensitivity {sensitivity!r}")
        check_payload_keys(payload)
        e = self.log.append(Entry(n=0, type=type, tag=tag, task_id=task_id, task_type=task_type, sensitivity=sensitivity, actor=actor, payload=payload, at=self.clock()))
        self._apply(e)
        for fn in list(self.listeners):
            fn(e, self)
        return e

    def _refuse(self, code: str, detail: str = "", *, task_id: str | None = None, sensitivity: str = "internal", actor: str = "GUARD") -> BoardError:
        """Record the refusal (GUARD) and hand back the error to raise. A refusal changes no state."""
        self.refusals += 1
        t = self.tasks.get(task_id) if task_id else None
        self._emit("board.refused", tag="GUARD", actor="GUARD", task_id=task_id, task_type=t["task_type"] if t else None, sensitivity=t["sensitivity"] if t else sensitivity,
                   payload={"refusal": code, "detail": detail, "attempted_by": actor})
        return BoardError(code, detail)

    def _apply(self, e: Entry) -> None:
        p, t = e.payload, e.type
        if t == "board.task":
            self.tasks[e.task_id] = {"task_id": e.task_id, "task_type": e.task_type, "sensitivity": e.sensitivity, "parent": p.get("parent"), "verifies": p.get("verifies"),
                                     "input": p["input"], "expected": p["expected"], "test": p["test"], "created_by": p["created_by"], "status": "open", "attempt": 0,
                                     "lease_id": None, "node_id": None, "route": None, "result": None, "free_reason": None, "worked_by": None,
                                     "route_refusal": None, "proposal": None, "escalation_step": 0, "results": [],
                                     "input_refs": p.get("input_refs", []), "exclude": p.get("exclude")}
        elif t == "board.route_decision":
            task, d = self.tasks[e.task_id], p["decision"]
            if d["escalation_step"] > task["escalation_step"] and task["status"] in ("failed", "unverified"):
                task.update(status="open", attempt=0, free_reason=None)  # an escalated combination is a fresh start; the earlier result stays in `results`
            task.update(route=d, route_refusal=None, proposal=None, escalation_step=d["escalation_step"])
            if d.get("effective_sensitivity") in RANK:  # a task is routed under the highest level of itself and its inputs, and the board keeps the highest
                task["sensitivity"] = highest(task["sensitivity"], d["effective_sensitivity"])
        elif t == "board.route_refusal":
            self.tasks[e.task_id].update(route_refusal=p["refusal"], proposal=None)
        elif t == "board.escalation_proposed":
            self.tasks[e.task_id]["proposal"] = {"decision": p["decision"], "decision_id": p["decision_id"]}
        elif t == "board.escalation_declined":
            self.tasks[e.task_id]["proposal"] = None
        elif t == "board.lease":
            self.leases[p["lease_id"]] = {"lease_id": p["lease_id"], "task_id": e.task_id, "node_id": p["node_id"], "deadline": p["deadline"], "attempt": p["attempt"], "state": "live",
                                          "extended_s": 0.0, "steps": 0, "end_reason": None}
            task = self.tasks[e.task_id]
            task.update(status="claimed", attempt=p["attempt"], lease_id=p["lease_id"], node_id=p["node_id"], free_reason=None)
        elif t == "board.progress":
            ls = self.leases[p["lease_id"]]
            ls["steps"] += 1
            if p.get("extend_s"):
                ls["deadline"] += p["extend_s"]
                ls["extended_s"] += p["extend_s"]
        elif t == "board.result":
            ls = self.leases[p["lease_id"]]
            ls.update(state="ended", end_reason="result")
            task = self.tasks[e.task_id]
            status = {True: "done", False: "failed", None: "unverified"}[p["test_result"].get("passed")]
            task["results"].append({"lease_id": p["lease_id"], "node_id": ls["node_id"], "environment_id": task["route"]["route"]["environment_id"] if task["route"] else None, "test_result": p["test_result"], "escalation_step": task["escalation_step"]})
            task.update(status=status, lease_id=None, result={"lease_id": p["lease_id"], "output": p["output"], "evidence": p["evidence"], "test_result": p["test_result"],
                                                               "evidence_status": p["evidence_status"], "node_id": ls["node_id"]}, worked_by=ls["node_id"])
        elif t in ("board.lease_expired", "board.lease_released"):
            ls = self.leases[p["lease_id"]]
            ls.update(state="ended", end_reason=p["reason"])
            task = self.tasks[e.task_id]
            task.update(status=p["task_status"], lease_id=None, node_id=None, free_reason=p["reason"])
        elif t == "board.node_card":
            self.cards[p["node_id"]] = p
        elif t == "board.published_skill":
            self.skills[p["hash"]] = p

    # ---- entries of the six types -----------------------------------------------------------------------------------------------------------

    def publish_node_card(self, card: dict, *, actor: str = "GUARD") -> Entry:
        with self._lock:
            missing = [k for k in REQUIRED["node_card"] if k not in card]
            if missing:
                raise self._refuse("bad_entry", f"node_card needs {missing}", actor=actor)
            for c in card["capabilities"]:
                if not (isinstance(c, dict) and c.get("name") and c.get("evidence_id") and c.get("date")):
                    raise self._refuse("untested_capability", str(c.get("name") if isinstance(c, dict) else c), actor=actor)
            return self._emit("board.node_card", tag=actor, actor=actor, task_id=None, task_type=None, sensitivity="internal", payload=dict(card))

    def publish_skill(self, skill: str, skill_hash: str, sensitivity: str, *, tests_passed: bool, actor: str = "GUARD") -> Entry:
        with self._lock:
            if not tests_passed:
                raise self._refuse("bad_entry", "a published skill passed its tests and the verifier", actor=actor)
            return self._emit("board.published_skill", tag=actor, actor=actor, task_id=None, task_type=None, sensitivity=sensitivity, payload={"skill": skill, "hash": skill_hash})

    def post_task(self, task_type: str, input: Any, expected: Any, test: Any, sensitivity: str, created_by: str, *, actor: str, parent: str | None = None,
                  verifies: str | None = None, task_id: str | None = None, input_refs: list | None = None, exclude: dict | None = None) -> str:
        """A typed task. Its sensitivity is the highest of itself and its parent / the task it verifies; lowering is refused. The task type is the creator's choice (10.5)."""
        with self._lock:
            if sensitivity not in SENSITIVITY:
                raise self._refuse("bad_entry", f"unknown sensitivity {sensitivity!r}", actor=actor)
            if not task_type:
                raise self._refuse("bad_entry", "task_type is required", actor=actor)
            for ref in (parent, verifies):
                if ref is not None:
                    src = self.tasks.get(ref)
                    if src is None:
                        raise self._refuse("unknown_task", ref, actor=actor)
                    if RANK[sensitivity] < RANK[src["sensitivity"]]:
                        raise self._refuse("sensitivity_lowered", f"{sensitivity} < {src['sensitivity']} of {ref}", task_id=ref, actor=actor)
            tid = task_id or f"task-{len(self.tasks) + 1}"
            if tid in self.tasks:
                raise self._refuse("bad_entry", f"task {tid} exists", actor=actor)
            self._emit("board.task", tag=actor, actor=actor, task_id=tid, task_type=task_type, sensitivity=sensitivity,
                       payload={"input": input, "expected": expected, "test": test, "created_by": created_by, **({"parent": parent} if parent else {}), **({"verifies": verifies} if verifies else {}),
                                **({"input_refs": list(input_refs)} if input_refs else {}), **({"exclude": exclude} if exclude else {})})
            return tid

    def record_route_decision(self, task_id: str, decision: dict, *, actor: str = "ROUTER") -> Entry:
        """Store a `RouteDecision` (docs/ROUTER_API.md section 1): `{task_id, route: {environment_id, subtype, model_id, access_id, machine_id}, rule_trace, escalation_step, evidence_label}`.
        The board does not read the trace. A decision for another task, an older escalation step or a malformed one is refused. A higher step on a failed task reopens it."""
        with self._lock:
            t = self._task(task_id, actor)
            ok = (isinstance(decision, dict) and decision.get("task_id") == task_id and isinstance(decision.get("route"), dict) and decision["route"].get("machine_id")
                  and isinstance(decision.get("rule_trace"), list) and decision.get("escalation_step") in (0, 1, 2) and decision.get("evidence_label"))
            if not ok:
                raise self._refuse("bad_entry", "a route decision names its task, route.machine_id, rule_trace, escalation_step and evidence_label", task_id=task_id, actor=actor)
            if decision["escalation_step"] < t["escalation_step"]:
                raise self._refuse("stale_decision", f"step {decision['escalation_step']} < {t['escalation_step']}", task_id=task_id, actor=actor)
            return self._emit("board.route_decision", tag=actor, actor=actor, task_id=task_id, task_type=t["task_type"], sensitivity=t["sensitivity"], payload={"decision": decision})

    def record_route_refusal(self, task_id: str, refusal: dict, *, actor: str = "GUARD") -> Entry:
        """Store a router `Refusal` (`{task_id, code, reason, details}`). The task stays where it is; nobody can take it without a decision."""
        with self._lock:
            t = self._task(task_id, actor)
            if not (isinstance(refusal, dict) and refusal.get("code") and refusal.get("reason")):
                raise self._refuse("bad_entry", "a refusal has a code and a reason", task_id=task_id, actor=actor)
            return self._emit("board.route_refusal", tag=actor, actor=actor, task_id=task_id, task_type=t["task_type"], sensitivity=t["sensitivity"], payload={"refusal": refusal})

    def propose_escalation(self, task_id: str, decision: dict, decision_id: str) -> Entry:
        """The router proposed a higher step; it waits here for the owner's answer to `decision_id` before it becomes the task's route."""
        with self._lock:
            t = self._task(task_id, "ROUTER")
            return self._emit("board.escalation_proposed", tag="ROUTER", actor="ROUTER", task_id=task_id, task_type=t["task_type"], sensitivity=t["sensitivity"],
                              payload={"decision": decision, "decision_id": decision_id})

    def decline_escalation(self, task_id: str, decision_id: str) -> Entry:
        with self._lock:
            t = self._task(task_id, "OWNER")
            return self._emit("board.escalation_declined", tag="GUARD", actor="GUARD", task_id=task_id, task_type=t["task_type"], sensitivity=t["sensitivity"], payload={"decision_id": decision_id})

    def note(self, type: str, task_id: str | None, payload: dict, *, tag: str = "GUARD") -> Entry:
        """A coordination record that is not one of the six message types and changes no board state (origin recorded, dispatch refused, run stopped)."""
        with self._lock:
            t = self.tasks.get(task_id) if task_id else None
            return self._emit("board." + type, tag=tag, actor=tag, task_id=task_id, task_type=t["task_type"] if t else None, sensitivity=t["sensitivity"] if t else "internal", payload=payload)

    def machine_of(self, task_id: str) -> str | None:
        r = self.tasks[task_id]["route"]
        return r["route"]["machine_id"] if r else None

    def active_leases(self, machine_id: str) -> int:
        """How many live leases a machine holds (the router reads this for its load step, ROUTER_API section 8)."""
        with self._lock:
            return sum(1 for ls in self.leases.values() if ls["state"] == "live" and ls["node_id"] == machine_id)

    def claim(self, task_id: str, node_id: str, *, actor: str, lease_s: float | None = None) -> str:
        """A worker takes a task. Refused (typed) when the task is not open, has no router decision for this machine, would be verified by the machine that did the work, or has no attempts left."""
        with self._lock:
            self.sweep()
            t = self._task(task_id, actor)
            if t["status"] == "claimed":
                raise self._refuse("already_claimed", f"held by {t['node_id']}", task_id=task_id, actor=actor)
            if t["status"] != "open":
                raise self._refuse("not_open", t["status"], task_id=task_id, actor=actor)
            if t["attempt"] >= self.max_attempts:
                raise self._refuse("attempts_exhausted", f"{t['attempt']} of {self.max_attempts}", task_id=task_id, actor=actor)
            if t["route"] is None:
                raise self._refuse("no_router_decision", task_id, task_id=task_id, actor=actor)
            if self.machine_of(task_id) != node_id:
                raise self._refuse("wrong_node_for_route", f"{node_id} is not {self.machine_of(task_id)}", task_id=task_id, actor=actor)
            if t["verifies"] and self.tasks[t["verifies"]]["worked_by"] == node_id:
                raise self._refuse("verifier_not_independent", node_id, task_id=task_id, actor=actor)
            return self._lease(t, node_id, lease_s, actor)

    def _lease(self, t: dict, node_id: str, lease_s: float | None, actor: str) -> str:
        secs = self.default_lease_s if lease_s is None else float(lease_s)
        if secs <= 0 or secs > self.max_lease_s:
            raise self._refuse("lease_too_long", f"{secs}s > {self.max_lease_s}s", task_id=t["task_id"], actor=actor)
        attempt = t["attempt"] + 1
        lid = f"lease-{len(self.leases) + 1}"
        self._emit("board.lease", tag=actor, actor=actor, task_id=t["task_id"], task_type=t["task_type"], sensitivity=t["sensitivity"],
                   payload={"lease_id": lid, "node_id": node_id, "deadline": self.clock() + secs, "attempt": attempt})
        return lid

    def progress(self, lease_id: str, step: str, note: str, *, actor: str, extend_s: float = 0.0) -> Entry:
        with self._lock:
            ls, t = self._live(lease_id, actor)
            if extend_s < 0 or ls["extended_s"] + extend_s > self.max_extend_s:
                raise self._refuse("extension_too_long", f"{ls['extended_s'] + extend_s}s > {self.max_extend_s}s", task_id=t["task_id"], actor=actor)
            return self._emit("board.progress", tag=actor, actor=actor, task_id=t["task_id"], task_type=t["task_type"], sensitivity=t["sensitivity"],
                              payload={"lease_id": lease_id, "step": step, "note": note, **({"extend_s": extend_s} if extend_s else {})})

    def result(self, lease_id: str, output: Any, evidence: list, test_result: dict | None, *, actor: str) -> Entry:
        """The worker's result. `test_result` is what CODE decided: `{passed: bool, by: "code", ...}`. Without it the output is stored as a hypothesis (`unverified`), never as tested."""
        with self._lock:
            ls, t = self._live(lease_id, actor)
            code_graded = isinstance(test_result, dict) and test_result.get("by") == "code" and isinstance(test_result.get("passed"), bool)
            tr = test_result if code_graded else {"passed": None, "by": None}
            return self._emit("board.result", tag=actor, actor=actor, task_id=t["task_id"], task_type=t["task_type"], sensitivity=t["sensitivity"],
                              payload={"lease_id": lease_id, "output": output, "evidence": list(evidence), "test_result": tr, "evidence_status": "tested" if code_graded else "hypothesis"})

    def release(self, lease_id: str, reason: str = "released_by_worker", *, actor: str) -> Entry:
        with self._lock:
            ls, t = self._live(lease_id, actor)
            return self._free(ls, t, reason, actor)

    def _free(self, ls: dict, t: dict, reason: str, actor: str, keep_open: bool = False) -> Entry:
        status = "open" if keep_open or t["attempt"] < self.max_attempts else "exhausted"
        return self._emit("board.lease_expired" if reason == "deadline_passed" else "board.lease_released", tag=actor, actor=actor, task_id=t["task_id"], task_type=t["task_type"],
                          sensitivity=t["sensitivity"], payload={"lease_id": ls["lease_id"], "reason": reason if status == "open" else "attempts_exhausted", "task_status": status,
                                                                  "attempts": t["attempt"]})

    def sweep(self) -> list[Entry]:
        """Free every lease whose deadline passed, with the typed reason `deadline_passed` (or `attempts_exhausted` when no attempt is left). Called before every claim and by the coordinator's timer."""
        with self._lock:
            now, out = self.clock(), []
            for ls in list(self.leases.values()):
                if ls["state"] == "live" and now >= ls["deadline"]:
                    out.append(self._free(ls, self.tasks[ls["task_id"]], "deadline_passed", "GUARD"))
            return out

    # ---- gated by the owner (see decisions.py) ----------------------------------------------------------------------------------------------

    def reassign(self, task_id: str, node_id: str, *, approval_event: Entry, lease_s: float | None = None) -> str:
        """Move a task to another machine. This changes what runs where, so it needs the OWNER's approval of exactly this change: `approval_event` comes from `DecisionDesk.require`."""
        with self._lock:
            logged = next((e for e in self.log.entries() if e.n == approval_event.n), None)  # a forged or edited entry is not the one in the log
            if logged is None or logged.tag != "OWNER" or logged.type != "board.decision_answered" or logged.payload.get("option") != "approve":
                raise self._refuse("no_owner_approval", f"reassign {task_id}", task_id=task_id, actor="OWNER")
            t = self._task(task_id, "OWNER")
            if t["status"] == "done":
                raise self._refuse("not_open", t["status"], task_id=task_id, actor="OWNER")
            if t["status"] == "claimed":
                self._free(self.leases[t["lease_id"]], t, "reassigned", "OWNER", keep_open=True)
                t = self.tasks[task_id]
            self._emit("board.route_decision", tag="OWNER", actor="OWNER", task_id=task_id, task_type=t["task_type"], sensitivity=t["sensitivity"],
                       payload={"decision": {"task_id": task_id, "route": {"environment_id": None, "subtype": "owner", "model_id": None, "access_id": None, "machine_id": node_id},
                                             "rule_trace": [], "escalation_step": t["escalation_step"], "evidence_label": "no data", "approval": approval_event.n}})
            return self._lease(self.tasks[task_id], node_id, lease_s, "OWNER")

    # ---- reads ------------------------------------------------------------------------------------------------------------------------------

    def _task(self, task_id: str, actor: str) -> dict:
        t = self.tasks.get(task_id)
        if t is None:
            raise self._refuse("unknown_task", task_id, actor=actor)
        return t

    def _live(self, lease_id: str, actor: str) -> tuple[dict, dict]:
        ls = self.leases.get(lease_id)
        if ls is None:
            raise self._refuse("unknown_lease", lease_id, actor=actor)
        if ls["state"] != "live":
            raise self._refuse("lease_not_live", ls["end_reason"] or "", task_id=ls["task_id"], actor=actor)
        if self.clock() >= ls["deadline"]:
            self.sweep()
            raise BoardError("lease_expired", lease_id)
        return ls, self.tasks[ls["task_id"]]

    def max_sensitivity(self, task_ids: list[str] | None = None) -> str:
        ts = [self.tasks[i] for i in task_ids] if task_ids is not None else list(self.tasks.values())
        return highest(*(t["sensitivity"] for t in ts))

    def view(self) -> dict:
        """The compact board for `show_data`: one row per task, no inputs or outputs."""
        now = self.clock()
        rows = []
        parents = {t["parent"] for t in self.tasks.values() if t["parent"]}
        for t in self.tasks.values():
            if t["task_id"] in parents:
                continue  # the owner's own task is not a line of work; its subtasks are
            ls = self.leases.get(t["lease_id"]) if t["lease_id"] else None
            rows.append({"task_id": t["task_id"], "task_type": t["task_type"], "sensitivity": t["sensitivity"], "status": t["status"], "node_id": t["node_id"], "attempt": t["attempt"],
                         "parent": t["parent"], "verifies": t["verifies"], "free_reason": t["free_reason"], "machine_id": self.machine_of(t["task_id"]),
                         "environment_id": t["route"]["route"]["environment_id"] if t["route"] else None, "escalation_step": t["escalation_step"],
                         "refusal": t["route_refusal"]["code"] if t["route_refusal"] else None,
                         "seconds_left": round(ls["deadline"] - now, 1) if ls else None, "evidence_status": (t["result"] or {}).get("evidence_status")})
        return {"tasks": rows, "counts": {s: sum(1 for r in rows if r["status"] == s) for s in ("open", "claimed", "done", "failed", "unverified", "exhausted")}}

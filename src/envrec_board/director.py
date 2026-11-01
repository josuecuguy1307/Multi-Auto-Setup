"""The director's closed menu and the only tools a model gets on the board.

The model (the director) picks ONE validated action per turn from a small closed menu. It decomposes an owner task into typed subtasks, asks for verification, accepts or rejects results, and closes.
It never picks a route, a machine, a model or a harness: no action has such an argument, and a menu action that carries one is refused (`forbidden_argument`). Where a subtask runs is the router's
decision (docs/ROUTER_API.md, SEAM `on_posted`); until the router is wired a posted subtask simply waits, and the board refuses any claim without a recorded decision.

`ModelTools` is the whole surface a chat model reaches. It has no `answer`, no `reassign`, no `claim`: an owner decision cannot come out of it.
This module is protocol and validation only: the loop that asks a model each turn is not built here (it needs the router).
"""

from __future__ import annotations

from typing import Any, Callable

from envrec_board.board import Board
from envrec_board.plan import PlanBook
from envrec_board.vocab import BoardError, Entry

MAX_SUBTASKS = 8
VERIFY_INSTRUCTIONS_REF = "instr:verify"  # the verifier's stored instructions (ROUTER_API section 11); the result under check is the task's input_refs[0], `result-of:<task>`
VERIFY_GRADER = {"kind": "regex", "value": r"^VERIFIED\b"}  # the code grader applied to a verifier's answer
NO_PROGRESS_TURNS = 5  # same rule as the harness loop (envrec.loop.run)
CLOSE_ON = ("finish", "escalate_to_owner")
FORBIDDEN_ARGS = ("route", "machine", "node", "node_id", "model", "harness", "worker", "host", "environment", "combination", "sensitivity", "decision", "approve", "owner")

SPEC: dict[str, dict[str, Any]] = {  # name -> required arguments (name -> allowed type or tuple of allowed values)
    "post_subtask": {"task_type": str, "input_ref": str, "expected": str, "test_ref": str},
    "request_verification": {"subtask_id": str},  # always another machine than the one that did the work: the board fills the router's `exclude`, the director cannot name one
    "submit_plan": {},  # ask the owner to approve the listed subtasks (and one verification slot for each); nothing unapproved is routed
    "accept_result": {"subtask_id": str},
    "reject_result": {"subtask_id": str, "reason": str},
    "finish": {"summary": str},
    "escalate_to_owner": {"question": str},
}


class DirectorRefused(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code, self.detail = code, detail
        super().__init__(f"{code}: {detail}" if detail else code)


class DirectorView:
    """What the director may do now, computed from the board. Read-only."""

    def __init__(self, board: Board, root_id: str, desk=None) -> None:
        self.board, self.root_id, self.desk = board, root_id, desk
        self.book = PlanBook(board, root_id)

    def decisions(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for e in self.board.log.entries():
            if e.type == "board.director_action" and e.payload["action"] in ("accept_result", "reject_result") and e.payload["args"].get("subtask_id"):
                out[e.payload["args"]["subtask_id"]] = "accepted" if e.payload["action"] == "accept_result" else "rejected"
        return out

    def subtasks(self) -> list[dict]:
        ts = self.board.tasks
        decided = self.decisions()
        subs = []
        for t in ts.values():
            if t["parent"] == self.root_id and not t["verifies"]:
                ver = [v for v in ts.values() if v["verifies"] == t["task_id"]]
                subs.append({**t, "verification": ver, "director_decision": decided.get(t["task_id"]), "approval": self.approval(t["task_id"])})
        return subs

    def approval(self, tid: str) -> str:
        """Where a task stands with the owner's plan: approved, declined, unapproved (needs submit_plan) or pending (the owner has the plan)."""
        if self.desk is None or self.book.has_origin(tid) or self.book.claim_for(tid):
            return "approved"
        if tid in self.book.declined(self.desk):
            return "declined"
        return "pending" if self.book.pending_plan(self.desk) else "unapproved"

    def verified(self, s: dict) -> bool:
        """A result counts as verified when code graded it, or a verification task (a different machine) finished with a passing code grade. A hypothesis alone never does."""
        if s["status"] == "done":
            return True
        return any(v["status"] == "done" for v in s["verification"])

    def menu(self) -> dict[str, dict]:
        subs = self.subtasks()
        offered: dict[str, dict] = {}
        if len(subs) < MAX_SUBTASKS:
            offered["post_subtask"] = SPEC["post_subtask"]
        resulted = [s for s in subs if s["status"] in ("done", "failed", "unverified") and not s["director_decision"]]
        need_ver = [s["task_id"] for s in resulted if s["status"] in ("done", "unverified") and not any(v["status"] != "exhausted" for v in s["verification"])]  # a code-graded result may be cross-checked too
        if need_ver:
            offered["request_verification"] = {**SPEC["request_verification"], "subtask_ids": need_ver}
        if any(self.verified(s) for s in resulted):
            offered["accept_result"] = {**SPEC["accept_result"], "subtask_ids": [s["task_id"] for s in resulted if self.verified(s)]}
        blocked = [s for s in subs if (s["route_refusal"] or s["approval"] == "declined") and s["status"] == "open" and not s["director_decision"]]  # the router refused it: nothing can run it
        if resulted or blocked:
            offered["reject_result"] = {**SPEC["reject_result"], "subtask_ids": [s["task_id"] for s in resulted + blocked]}
        if subs and all(s["director_decision"] or s["status"] == "exhausted" for s in subs):
            offered["finish"] = SPEC["finish"]
        if self.desk is not None and not self.book.pending_plan(self.desk) and self.book.unapproved(self.desk):
            offered["submit_plan"] = {**SPEC["submit_plan"], "tasks_waiting": self.book.unapproved(self.desk)}
        offered["escalate_to_owner"] = SPEC["escalate_to_owner"]
        return offered


def validate_action(action: Any, view: DirectorView) -> tuple[str, dict]:
    """Check one action against the closed menu. Raises DirectorRefused with a typed code; returns (name, args)."""
    if not isinstance(action, dict) or set(action) - {"action", "args"} or not isinstance(action.get("action"), str):
        raise DirectorRefused("bad_shape", "an action is {'action': name, 'args': {...}}")
    name, args = action["action"], action.get("args", {})
    if not isinstance(args, dict):
        raise DirectorRefused("bad_shape", "args must be an object")
    if name not in SPEC:
        raise DirectorRefused("unknown_action", name)
    for k in args:
        if any(f == k.lower() or f in k.lower().split("_") for f in FORBIDDEN_ARGS):
            raise DirectorRefused("forbidden_argument", f"{k}: the director never picks routes, machines or models, and never decides for the owner")
    spec = SPEC[name]
    extra = set(args) - set(spec)
    if extra:
        raise DirectorRefused("unknown_argument", ", ".join(sorted(extra)))
    for k, kind in spec.items():
        if k not in args:
            raise DirectorRefused("missing_argument", k)
        ok = args[k] in kind if isinstance(kind, tuple) else isinstance(args[k], kind) and bool(str(args[k]).strip())
        if not ok:
            raise DirectorRefused("bad_argument", f"{k} must be {'one of ' + str(kind) if isinstance(kind, tuple) else 'a non-empty string'}")
    menu = view.menu()
    if name not in menu:
        raise DirectorRefused("not_offered", f"{name} is not offered now; offered: {', '.join(menu)}")
    ids = menu[name].get("subtask_ids")
    if ids is not None and args.get("subtask_id") not in ids:
        raise DirectorRefused("not_offered", f"{name} on {args.get('subtask_id')!r}; offered for: {', '.join(ids)}")
    return name, args


class ModelTools:
    """The director's hands: validated actions only. `on_posted(task_id)` is the SEAM where the router is asked for a decision (T4a); None until ROUTER_API.md is wired."""

    def __init__(self, board: Board, root_id: str, *, desk=None, on_submit_plan: Callable[[], dict] | None = None, on_posted: Callable[[str], None] | None = None) -> None:
        self.board, self.root_id, self.on_posted, self.on_submit_plan = board, root_id, on_posted, on_submit_plan
        self.view = DirectorView(board, root_id, desk)
        self.stop: str | None = None
        self.idle_turns = 0
        self._seen: set[str] = set()

    def _note(self, name: str, args: dict, **extra) -> Entry:
        r = self.board.tasks[self.root_id]
        return self.board._emit("board.director_action", tag="MODEL", actor="MODEL", task_id=self.root_id, task_type=r["task_type"], sensitivity=r["sensitivity"],
                                payload={"action": name, "args": args, **extra})

    def act(self, action: Any) -> dict:
        """One turn. Returns {ok, code?, progress, stop?}. A refused, invalid or repeated-identical action adds no progress; five in a row end the protocol as `no_progress`."""
        if self.stop:
            return {"ok": False, "code": "protocol_closed", "progress": False, "stop": self.stop}
        key = repr(sorted(action.items())) if isinstance(action, dict) and isinstance(action.get("args", {}), dict) else repr(action)
        try:
            name, args = validate_action(action, self.view)
            if args and key in self._seen:  # an action without arguments (submit_plan) is judged by the menu: it is offered only while something waits
                raise DirectorRefused("repeated_action", "the same action again adds nothing")
        except DirectorRefused as r:
            return self._idle(r.code, r.detail)
        self._seen.add(key)
        root = self.board.tasks[self.root_id]
        if name == "post_subtask":
            tid = self.board.post_task(args["task_type"], args["input_ref"], args["expected"], args["test_ref"], root["sensitivity"], "director", actor="MODEL", parent=self.root_id)
            if self.on_posted:
                self.on_posted(tid)
            out = {"task_id": tid}
        elif name == "request_verification":
            src = self.board.tasks[args["subtask_id"]]
            last = src["results"][-1]  # the work being checked: the verifier must not be that machine or that environment (ROUTER_API section 11)
            tid = self.board.post_task("verification", VERIFY_INSTRUCTIONS_REF, "the verifier confirms the result", "verifier-says-VERIFIED", src["sensitivity"], "director", actor="MODEL",
                                       parent=self.root_id, verifies=src["task_id"], input_refs=[f"result-of:{src['task_id']}"],
                                       exclude={"machine_ids": [last["node_id"]], **({"environment_ids": [last["environment_id"]]} if last.get("environment_id") else {})})
            if self.on_posted:
                self.on_posted(tid)
            out = {"task_id": tid}
        elif name == "submit_plan":
            d = self.on_submit_plan() if self.on_submit_plan else None
            self._note(name, args, **({"decision_id": d["decision_id"]} if d else {}))
            out = {"decision_id": d["decision_id"]} if d else {}
        else:
            self._note(name, args)
            out = {}
            if name in CLOSE_ON:
                self.stop = "finished" if name == "finish" else "blocked_needs_human"
        self.idle_turns = 0
        return {"ok": True, "progress": True, **out, **({"stop": self.stop} if self.stop else {})}

    def _idle(self, code: str, detail: str) -> dict:
        self.idle_turns += 1
        r = self.board.tasks[self.root_id]
        self.board._emit("board.director_refused", tag="GUARD", actor="GUARD", task_id=self.root_id, task_type=r["task_type"], sensitivity=r["sensitivity"], payload={"refusal": code, "detail": detail})
        if self.idle_turns >= NO_PROGRESS_TURNS:
            self.stop = "no_progress"
        return {"ok": False, "code": code, "detail": detail, "progress": False, **({"stop": self.stop} if self.stop else {})}

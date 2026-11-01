"""The owner's approved plans for a director's subtasks (docs/ROUTER_API.md section 12).

A plan is ONE pending decision (kind `decompose`) that lists exactly what the owner is asked to allow: each subtask (id, type, sensitivity, input reference, test reference) and, for each listed subtask,
one verification slot. When the owner answers it by a human gesture the router writes the OWNER event (`approve_decomposition`) and the plan is recorded here (`board.plan_approved`). A task runs only
if it is a listed subtask, or a verification of a listed subtask that still has its slot and is not lower in sensitivity than the subtask. Anything else (a new subtask, a second verification of the same
subtask) is unapproved: it waits until the director submits a new plan and the owner approves it. An escalation inside a task does not need a new plan.
"""

from __future__ import annotations

from envrec_board.board import Board
from envrec_board.vocab import RANK


class PlanBook:
    def __init__(self, board: Board, root_id: str) -> None:
        self.board, self.root_id = board, root_id

    def _entries(self, type: str):
        return [e for e in self.board.log.entries() if e.type == type and e.task_id == self.root_id]

    def family(self) -> list[dict]:
        return [t for t in self.board.tasks.values() if t["task_id"] != self.root_id and (t["parent"] == self.root_id or t["verifies"])]

    def plans(self) -> list[dict]:
        return [e.payload for e in self._entries("board.plan_approved")]

    def pending_plan(self, desk) -> str | None:
        """The decision id of a plan the owner has not answered yet."""
        for e in self._entries("board.decision_pending"):
            if e.payload["kind"] == "decompose" and desk.answer_of(e.payload["decision_id"]) is None:
                return e.payload["decision_id"]
        return None

    def declined(self, desk) -> set[str]:
        """Tasks that were in a plan the owner rejected and have no origin since."""
        out: set[str] = set()
        for e in self._entries("board.decision_pending"):
            a = desk.answer_of(e.payload["decision_id"]) if e.payload["kind"] == "decompose" else None
            if a is not None and a.payload["option"] == "reject":
                out |= {i["task_id"] for i in e.payload["details"]["items"] if i["task_id"]}
        return {t for t in out if not self.has_origin(t)}

    def origins(self) -> list[dict]:
        return [e.payload | {"task_id": e.task_id} for e in self.board.log.entries() if e.type == "board.origin"]

    def has_origin(self, tid: str) -> bool:
        return any(o["task_id"] == tid for o in self.origins())

    def claim_for(self, tid: str) -> tuple[dict, dict] | None:
        """(plan, item) the task can rest on, or None. A listed subtask rests on its own item; a verification rests on its subtask's unused slot."""
        t = self.board.tasks[tid]
        for plan in self.plans():
            for item in plan["items"]:
                if item["task_id"] == tid and item["kind"] in ("subtask", "verification"):
                    return plan, item
        if t["verifies"]:
            src = t["verifies"]
            used = {(o["decomposition_id"], o.get("slot_for")) for o in self.origins()}
            for plan in self.plans():
                for item in plan["items"]:
                    if item["kind"] == "verification_slot" and item["for"] == src and (plan["decomposition_id"], src) not in used and RANK[t["sensitivity"]] >= RANK[item["sensitivity"]]:
                        return plan, item
        return None

    def unapproved(self, desk) -> list[str]:
        """Family tasks with no origin, no plan item and no slot, that the owner has not already declined."""
        dec = self.declined(desk)
        return [t["task_id"] for t in self.family() if not self.has_origin(t["task_id"]) and self.claim_for(t["task_id"]) is None and t["task_id"] not in dec and t["status"] == "open"]

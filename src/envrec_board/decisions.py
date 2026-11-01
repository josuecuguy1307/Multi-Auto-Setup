"""Owner decisions on the board. Anything that changes what runs where (reassign, escalate beyond what the owner pre-approved, any write on a machine) is a pending decision that only a human
gesture can answer: the OS approval dialog in Claude Desktop, the native confirmation in Open WebUI, or the owner's panel. This is the same path as the setup approvals (docs/SETUP_API.md section 3).

* A decision is bound to a hash of exactly what it approves (kind, task, details). An approval of one is not an approval of another, and it is used once.
* `answer` needs a `channel` that is one of the human channels. A chat model's tool call has no such channel, and the model-facing tools (`director.ModelTools`) have no `answer` at all.
* `by="claude-code"` is recorded as the operator's (tag CLAUDE-CODE) and never counts. Any other `by` is refused.
* A decision nobody answers expires after 15 minutes and nothing is changed.
"""

from __future__ import annotations

from typing import Callable

from envrec_board import strings
from envrec_board.board import Board, canonical_hash
from envrec_board.vocab import BoardError, Entry

HUMAN_CHANNELS = ("os_dialog", "host_dialog", "panel")  # where a person's gesture happens; the adapters (T2b) pass one of these only after a real click
KINDS = ("reassign", "escalate_beyond_preapproved", "machine_write", "decompose")
TTL_S = 900.0


def decision_hash(kind: str, task_id: str | None, details: dict) -> str:
    return canonical_hash({"kind": kind, "task_id": task_id, "details": details})


class DecisionDesk:
    def __init__(self, board: Board, ttl_s: float = TTL_S) -> None:
        self.board, self.ttl_s = board, ttl_s
        self.hooks: dict[str, Callable[[Entry, Entry], None]] = {}  # kind -> fn(decision entry, OWNER answer entry), run inside the human-gesture answer

    # ---- ask --------------------------------------------------------------------------------------------------------------------------------

    def ask(self, kind: str, task_id: str | None, summary: str, details: dict, *, options: list[dict] | None = None, command: str | None = None, undo: str | None = None) -> dict:
        """Put a decision to the owner. A `machine_write` shows the exact command and how to undo it (and so is bound to a plan hash)."""
        b = self.board
        with b._lock:
            if kind not in KINDS:
                raise b._refuse("bad_entry", f"unknown decision kind {kind!r}", task_id=task_id)
            if kind == "machine_write" and not (command and undo):
                raise b._refuse("bad_entry", "a machine write shows its exact command and its undo", task_id=task_id)
            t = b.tasks.get(task_id) if task_id else None
            did = f"decision-{sum(1 for e in b.log.entries() if e.type == 'board.decision_pending') + 1}"
            opts = options or [{"id": "approve", "label": strings.OPTION_APPROVE}, {"id": "reject", "label": strings.OPTION_REJECT}]
            d = {"decision_id": did, "kind": kind, "hash": decision_hash(kind, task_id, details), "summary": summary, "details": details, "options": opts, "writes": kind == "machine_write",
                 "command": command, "undo": undo, "expires_in_s": self.ttl_s}
            b._emit("board.decision_pending", tag="GUARD", actor="GUARD", task_id=task_id, task_type=t["task_type"] if t else None, sensitivity=t["sensitivity"] if t else "internal", payload=d)
            return d

    def ask_reassign(self, task_id: str, to_node: str, summary: str) -> dict:
        return self.ask("reassign", task_id, summary, {"to_node": to_node})

    # ---- answer (human channels only) ---------------------------------------------------------------------------------------------------------

    def _pending(self, decision_id: str) -> tuple[Entry | None, list[Entry]]:
        es = self.board.log.entries()
        ask = next((e for e in es if e.type == "board.decision_pending" and e.payload["decision_id"] == decision_id), None)
        return ask, [e for e in es if e.type in ("board.decision_answered", "board.decision_used") and e.payload.get("decision_id") == decision_id]

    def answer(self, decision_id: str, option: str, *, channel: str, by: str) -> Entry:  # no default for `by`: a bare answer never counts as the owner's
        b = self.board
        with b._lock:
            ask, later = self._pending(decision_id)
            if ask is None:
                raise b._refuse("decision_unknown", decision_id, actor=by)
            tid = ask.task_id
            if any(e.tag == "OWNER" and e.type == "board.decision_answered" for e in later):
                raise b._refuse("decision_closed", decision_id, task_id=tid, actor=by)
            if b.clock() - ask.at > self.ttl_s:
                raise b._refuse("decision_expired", decision_id, task_id=tid, actor=by)
            if channel not in HUMAN_CHANNELS or by not in ("owner", "claude-code"):
                raise b._refuse("not_a_human_channel", f"channel={channel} by={by}", task_id=tid, actor=by)
            if option not in [o["id"] for o in ask.payload["options"]]:
                raise b._refuse("bad_entry", f"option {option!r} is not offered", task_id=tid, actor=by)
            tag = "OWNER" if by == "owner" else "CLAUDE-CODE"
            e = b._emit("board.decision_answered", tag=tag, actor=tag, task_id=tid, task_type=ask.task_type, sensitivity=ask.sensitivity,
                        payload={"decision_id": decision_id, "option": option, "channel": channel, "hash": ask.payload["hash"]})
            if tag == "OWNER" and option == "approve" and ask.payload["kind"] in self.hooks:
                self.hooks[ask.payload["kind"]](ask, e)  # only a human gesture reaches this line: the router's owner events are written from here
            return e

    # ---- require (what the board's gated operations call) --------------------------------------------------------------------------------------

    def require(self, decision_id: str, kind: str, task_id: str | None, details: dict, *, limit: int = 1) -> Entry:
        """The OWNER's `approve` for exactly this kind, task and details, `limit` times (once unless the owner approved a bounded batch, like a decomposition). An answer tagged CLAUDE-CODE, a rejection, another decision's approval or a used one is not enough."""
        b = self.board
        with b._lock:
            ask, later = self._pending(decision_id)
            want = decision_hash(kind, task_id, details)
            ok = next((e for e in later if e.type == "board.decision_answered" and e.tag == "OWNER" and e.payload["option"] == "approve" and e.payload["hash"] == want), None)
            if ask is None or ok is None or sum(1 for e in later if e.type == "board.decision_used") >= limit:
                raise b._refuse("no_owner_approval", f"{kind} {task_id}", task_id=task_id, actor="OWNER")
            b._emit("board.decision_used", tag="GUARD", actor="GUARD", task_id=task_id, task_type=ask.task_type, sensitivity=ask.sensitivity, payload={"decision_id": decision_id})
            return ok

    def reassign(self, task_id: str, to_node: str, decision_id: str, *, lease_s: float | None = None) -> str:
        ok = self.require(decision_id, "reassign", task_id, {"to_node": to_node})
        return self.board.reassign(task_id, to_node, approval_event=ok, lease_s=lease_s)

    def answer_of(self, decision_id: str) -> Entry | None:
        """The OWNER's answer to a decision, or None while the owner has not answered (the operator's answer is not one)."""
        _, later = self._pending(decision_id)
        return next((e for e in later if e.type == "board.decision_answered" and e.tag == "OWNER"), None)

    def pending(self) -> list[dict]:
        """Decisions still waiting for the owner (not answered by the OWNER, not expired)."""
        es = self.board.log.entries()
        done = {e.payload["decision_id"] for e in es if e.type == "board.decision_answered" and e.tag == "OWNER"}
        now = self.board.clock()
        return [dict(e.payload, task_id=e.task_id, sensitivity=e.sensitivity) for e in es if e.type == "board.decision_pending" and e.payload["decision_id"] not in done and now - e.at <= self.ttl_s]

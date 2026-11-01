"""Board changes as intents for the owner's chat (docs/SETUP_API.md, T2b's intent schema: kind, run_id, actor, sensitivity, payload, text_fallback).

Every board change becomes intents: `progress` (task posted, routed, taken, running, done, failed, freed), `show_data` with `data_kind: "board"` (compact state), `confirm` / `ask_choice` only when the
owner must decide, and `text` for refusals. Every intent has a Spanish `text_fallback` that is enough on its own; nothing depends on cards.

Delivery is per sink. A `cloud` sink (Claude Desktop, Meta AI later) is fail-closed: confidential intents never reach it (the owner gets a content-free notice), and with no outbound filter installed only
`public` ones do. T4a's outbound filter plugs in as `outbound_filter` (SEAM, not built here). The validator below mirrors T2b's `intents.validate`; when T2b merges, `validate=` takes theirs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from envrec_board import strings
from envrec_board.board import Board
from envrec_board.decisions import DecisionDesk
from envrec_board.vocab import ACTORS, RANK, SENSITIVITY, BoardError, Entry

KINDS = ("ask_choice", "confirm", "progress", "show_data", "text")
MAX_FALLBACK = 6000
OUTBOUND_FILTER_SEAM = "router.filter_outbound(intent, entry_chat) -> Intent (docs/ROUTER_API.md section 5)"


class IntentInvalid(ValueError):
    pass


def validate_intent(i: dict) -> dict:
    if i["kind"] not in KINDS:
        raise IntentInvalid(f"unknown kind {i['kind']!r}")
    if i["actor"] not in ACTORS:
        raise IntentInvalid(f"unknown actor {i['actor']!r}")
    if i["sensitivity"] not in SENSITIVITY:
        raise IntentInvalid(f"unknown sensitivity {i['sensitivity']!r}")
    p = i["payload"]
    if not isinstance(p, dict):
        raise IntentInvalid("payload must be an object")
    fb = i["text_fallback"]
    if not isinstance(fb, str) or not fb.strip() or len(fb) > MAX_FALLBACK:
        raise IntentInvalid("every intent needs a text fallback (1..6000 characters)")
    if i["kind"] == "show_data" and p.get("data_kind") != "board":
        raise IntentInvalid("the board only emits show_data with data_kind 'board'")
    if i["kind"] in ("ask_choice", "confirm"):
        if not isinstance(p.get("writes"), bool):
            raise IntentInvalid("must say whether it writes")
    if i["kind"] == "ask_choice":
        opts = p.get("options")
        if not isinstance(opts, list) or not opts or not all(isinstance(o, dict) and o.get("id") and o.get("label") for o in opts):
            raise IntentInvalid("ask_choice needs options with id and label")
        for n, o in enumerate(opts, 1):
            if f"{n}. {o['label']}" not in fb:
                raise IntentInvalid(f"the text fallback must list option {n} as a numbered line")
    if i["kind"] == "confirm" and p["writes"]:
        for need in ("summary", "command", "undo"):
            if not isinstance(p.get(need), str) or not p[need].strip():
                raise IntentInvalid(f"a confirmation that writes must show its exact {need}")
        if not isinstance(p.get("plan_hash"), str) or not p["plan_hash"].startswith("sha256:") or len(p["plan_hash"]) < 15:
            raise IntentInvalid("a confirmation that writes is bound to a plan hash")
    return i


@dataclass
class Sink:
    name: str
    kind: str  # "local" (Open WebUI with a local model, the panel) or "cloud" (a provider's chat)
    send: Callable[[dict], None]
    entry_chat: dict | None = None  # {kind, name} as docs/ROUTER_API.md section 1 defines it; filled from name/kind when absent


def board_text(view: dict) -> str:
    c = view["counts"]
    if not any(c.values()):
        return strings.BOARD_EMPTY
    lines = [strings.BOARD_HEAD + ", ".join(f"{c[k]} {strings.STATE_TEXT[k]}" for k in c if c[k])]
    for r in view["tasks"]:
        who = f" · {r['node_id']}" if r["node_id"] else ""
        left = strings.left_text(r["seconds_left"]) if r["seconds_left"] is not None else ""
        why = f" · {strings.FREE_REASONS.get(r['free_reason'], r['free_reason'])}" if r["free_reason"] and r["status"] in ("open", "exhausted") else ""
        lines.append(strings.task_line(r["task_id"], r["task_type"], r["sensitivity"], strings.STATE_TEXT[r["status"]], who, left, why))
    return "\n".join(lines)


class IntentEmitter:
    def __init__(self, board: Board, desk: DecisionDesk | None = None, sinks: list[Sink] | None = None, *, outbound_filter: Callable[[dict, dict], dict] | None = None,
                 validate: Callable[[dict], dict] = validate_intent, run_id: str = "board") -> None:
        self.board, self.desk, self.sinks, self.outbound_filter, self.validate, self.run_id = board, desk, list(sinks or []), outbound_filter, validate, run_id
        self.emitted: list[dict] = []  # every intent produced, before delivery
        self.withheld: list[dict] = []  # what a cloud sink did not get: {intent_id, sink, reason}
        self._seq = 0
        board.listeners.append(self.on_entry)

    # ---- building ---------------------------------------------------------------------------------------------------------------------------

    def _make(self, kind: str, actor: str, sensitivity: str, payload: dict, text: str) -> dict:
        self._seq += 1
        i = {"id": f"board-{self._seq}", "seq": self._seq, "ts": self.board.clock(), "kind": kind, "run_id": self.run_id, "actor": actor, "sensitivity": sensitivity, "payload": payload,
             "text_fallback": text}
        self.validate(i)
        self.emitted.append(i)
        self._deliver(i)
        return i

    def _progress(self, e: Entry, state: str, text: str, **extra) -> dict:
        t = self.board.tasks.get(e.task_id, {})
        return self._make("progress", e.tag, e.sensitivity, {"state": state, "task_id": e.task_id, "task_type": e.task_type, "node_id": t.get("node_id"), "attempt": t.get("attempt"), **extra}, text)

    def show_board(self, actor: str = "GUARD") -> dict:
        v = self.board.view()
        return self._make("show_data", actor, self.board.max_sensitivity(), {"data_kind": "board", "board": v}, board_text(v))

    def on_entry(self, e: Entry, board: Board) -> None:
        p, t = e.payload, e.type
        task = f"{e.task_id} ({e.task_type})"
        S = strings
        if t == "board.task":
            self._progress(e, "posted", S.owner_task(task) if (e.tag == "OWNER" and not p.get("parent") and not p.get("verifies")) else S.posted(task), parent=p.get("parent"), verifies=p.get("verifies"))
            self.show_board()
        elif t == "board.route_decision":
            d = p["decision"]
            r = d["route"]
            if d["escalation_step"] > 0:
                self._progress(e, "escalated", S.escalated(task, r["environment_id"], r["machine_id"], d["escalation_step"], d["evidence_label"]), escalation_step=d["escalation_step"])
            else:
                self._progress(e, "routed", S.routed(task, r["environment_id"], r["machine_id"], d["evidence_label"]), escalation_step=0)
            self.show_board()
        elif t == "board.route_refusal":
            f = p["refusal"]
            self._make("text", "GUARD", e.sensitivity, {"code": f["code"], "task_id": e.task_id}, S.route_refused(task, f["code"], f["reason"]))
        elif t == "board.escalation_declined":
            self._make("text", "GUARD", e.sensitivity, {"code": "escalation_declined", "task_id": e.task_id}, S.escalation_declined(task))
        elif t == "board.lease":
            self._progress(e, "taken", S.taken(p["node_id"], task, p["attempt"], p["deadline"] - e.at), lease_id=p["lease_id"], deadline=p["deadline"])
            self.show_board()
        elif t == "board.progress":
            self._progress(e, "running", S.running(task, p["step"], p["note"], p.get("extend_s")), lease_id=p["lease_id"], step=p["step"], note=p["note"])
        elif t == "board.result":
            passed = p["test_result"]["passed"]
            state, text, ev = {True: ("done", S.done, "tested"), False: ("failed", S.failed, "tested"), None: ("unverified", S.unverified, "hypothesis")}[passed]
            self._progress(e, state, text(task), lease_id=p["lease_id"], evidence_status=ev)
            self.show_board()
        elif t in ("board.lease_expired", "board.lease_released"):
            ex = p["task_status"] == "exhausted"
            self._progress(e, "freed", S.freed(task, ex, S.FREE_REASONS.get(p["reason"], p["reason"])), lease_id=p["lease_id"], reason=p["reason"], task_status=p["task_status"])
            self.show_board()
            if ex:
                self._make("text", "GUARD", e.sensitivity, {"code": "attempts_exhausted", "task_id": e.task_id}, S.exhausted_needs_owner(task))
        elif t == "board.director_action":
            a = p["action"]
            tail = f" {p['args']['subtask_id']}" if "subtask_id" in p["args"] else (f": {p['args']['question']}" if a == "escalate_to_owner" else (f": {p['args']['summary']}" if a == "finish" else ""))
            self._progress(e, "director_" + a, S.director(a, tail), action=a)
        elif t == "board.grade_refused":
            self._make("text", "GUARD", e.sensitivity, {"code": p["refusal"], "task_id": e.task_id}, S.grade_refused(task, p["refusal"], p["reason"]))
        elif t == "board.plan_refused":
            self._make("text", "GUARD", e.sensitivity, {"code": p["refusal"], "decision_id": p["decision_id"]}, S.plan_refused(p["reason"]))
        elif t == "board.plan_approved":
            self._make("text", "GUARD", e.sensitivity, {"code": "plan_approved", "decomposition_id": p["decomposition_id"]}, S.plan_approved(len(p["items"])))
        elif t == "board.run_stopped":
            self._make("text", "GUARD", e.sensitivity, {"code": "run_stopped", "stop": p["stop"]}, S.run_stopped(p["stop"]))
        elif t == "board.dispatched":
            self._progress(e, "dispatched", S.dispatched(task, p["machine_id"]), machine_id=p["machine_id"])
        elif t == "board.dispatch_refused":
            self._make("text", "GUARD", e.sensitivity, {"code": p["refusal"], "task_id": e.task_id}, S.dispatch_refused(task, p["refusal"], p["reason"]))
        elif t == "board.refused":
            self._make("text", "GUARD", e.sensitivity, {"code": p["refusal"], "task_id": e.task_id}, BoardError(p["refusal"], p.get("detail", "")).text)
        elif t == "board.decision_pending":
            self._decision(e)
        elif t == "board.decision_answered":
            if e.tag == "OWNER":
                word = S.OWNER_WORDS.get(p["option"]) or S.owner_chose(p["option"])
                self._progress(e, "owner_decided", S.owner_decided(word, p["decision_id"]), decision_id=p["decision_id"], option=p["option"])
            else:
                self._make("text", "GUARD", e.sensitivity, {"code": "not_owner", "decision_id": p["decision_id"]}, S.operator_answer(p["decision_id"]))

    def _decision(self, e: Entry) -> None:
        d = e.payload
        opts, minutes = d["options"], d["expires_in_s"] / 60
        if d["kind"] != "machine_write" and {o["id"] for o in opts} == {"approve", "reject"} and len(opts) == 2:
            payload = {"writes": False, "decision_id": d["decision_id"], "kind": d["kind"], "summary": d["summary"], "options": opts, "hash": d["hash"], "task_id": e.task_id}
            self._make("confirm", "GUARD", e.sensitivity, payload, strings.confirm_plain(d["summary"], d["decision_id"], minutes))
        elif d["kind"] == "machine_write":
            payload = {"writes": True, "decision_id": d["decision_id"], "kind": d["kind"], "summary": d["summary"], "command": d["command"], "undo": d["undo"], "plan_hash": d["hash"],
                       "options": opts, "task_id": e.task_id}
            self._make("confirm", "GUARD", e.sensitivity, payload, strings.confirm_write(d["summary"], d["command"], d["undo"], d["decision_id"], minutes))
        else:
            lines = "\n".join(f"{n}. {o['label']}" for n, o in enumerate(opts, 1))
            self._make("ask_choice", "GUARD", e.sensitivity, {"writes": False, "decision_id": d["decision_id"], "kind": d["kind"], "options": opts, "task_id": e.task_id},
                       strings.ask_choice(d["summary"], lines, d["decision_id"]))

    # ---- delivery ---------------------------------------------------------------------------------------------------------------------------

    def _deliver(self, i: dict) -> None:
        for s in self.sinks:
            if s.kind == "local":
                s.send(i)
                continue
            chat = s.entry_chat or {"kind": s.kind, "name": s.name}
            out, reason = i, None
            if self.outbound_filter is not None:  # T4a's filter is the authority: every intent for a cloud chat goes through it, confidential ones included
                out = self.outbound_filter(i, chat)
                if i["sensitivity"] == "confidential" and not out.get("redacted"):
                    reason = "not_redacted"  # fail closed: a confidential intent leaves only as the filter's redacted form
            elif i["sensitivity"] == "confidential":
                reason = "confidential"
            elif RANK[i["sensitivity"]] > RANK["public"]:
                reason = "no_outbound_filter"
            if reason:
                self.withheld.append({"intent_id": i["id"], "sink": s.name, "reason": reason})
                s.send({"id": i["id"] + "-notice", "seq": i["seq"], "ts": i["ts"], "kind": "text", "run_id": i["run_id"], "actor": "GUARD", "sensitivity": "public", "payload": {"code": "withheld"},
                        "text_fallback": strings.WITHHELD_NOTICE})
            else:
                s.send(out)

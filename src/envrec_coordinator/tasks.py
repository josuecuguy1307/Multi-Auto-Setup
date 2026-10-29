"""The coordinator's side of tasks (protocol-task, docs/NETWORK_CONTRACT.md 3.7). A task is made by the OWNER (the CLI or the web screen) or by code (`node_report` when a node connects), is
sent to ONE connected node inside that node's session with a capability token, and ends in exactly one terminal state. Every move is walked by the enforced protocol guard; every task and
result is an event (OWNER or GUARD). A node that is not connected gets a failed task at once with the typed reason `node_offline`: tasks are NOT queued (a queued task would run later, in
a session its token was not made for)."""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from envrec_net import task as T
from envrec_net.protocol import IllegalTransition, ProtocolGuard

MAX_PER_NODE = 2
ALERT_SKEW_S = 300.0


@dataclass
class TaskRecord:
    id: str
    node_id: str
    task_type: str
    by: str  # OWNER | GUARD
    created: float
    status: str = "created"
    reason: str = ""
    result: dict = field(default_factory=dict)
    evidence: list = field(default_factory=list)
    duration_s: float = 0.0
    finished: float | None = None
    guard: ProtocolGuard | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)

    def view(self) -> dict:
        return {"id": self.id, "node": self.node_id, "type": self.task_type, "by": self.by, "status": self.status, "reason": self.reason, "created": self.created,
                "finished": self.finished, "duration_s": self.duration_s}


def evaluate_report(report: dict, skew_s: float | None = None, *, alert_skew_s: float = ALERT_SKEW_S) -> list[dict]:
    """The checks the coordinator makes on a node's self-report. Each alert is {kind, text, ...}; an empty list means all is well."""
    alerts = []
    socks = report.get("sockets", [])
    if socks:
        alerts.append({"kind": "listening_ports", "ports": [f"{s.get('proto')}:{s.get('ip')}:{s.get('port')}" for s in socks][:12],
                       "text": "This node has listening ports; a node must have none."})
    key = report.get("key", {})
    if key.get("exists") and key.get("owner_only") is False:
        alerts.append({"kind": "key_not_private", "text": "The node's identity key file can be read by other users of that computer."})
    if skew_s is not None and abs(skew_s) > alert_skew_s:
        alerts.append({"kind": "clock_skew", "skew_s": round(skew_s), "text": f"The clock of this node differs by {round(abs(skew_s) / 60)} min (the connection still works)."})
    return alerts


class TaskDesk:
    def __init__(self, *, clock: Callable[[], float] = time.time, log: Callable[..., object], sessions: Callable[[str], object | None], max_per_node: int = MAX_PER_NODE) -> None:
        self.clock, self.log, self.sessions, self.max_per_node = clock, log, sessions, max_per_node
        self.tasks: dict[str, TaskRecord] = {}
        self.on_result: Callable[[TaskRecord], None] = lambda rec: None
        self.on_change: Callable[[], None] = lambda: None

    def list(self) -> list[TaskRecord]:
        return sorted(self.tasks.values(), key=lambda t: t.created)

    def _move(self, rec: TaskRecord, to: str, **facts) -> None:
        for k, v in facts.items():
            rec.guard.record(k, v)  # type: ignore[union-attr]
        try:
            rec.guard.advance(to)  # type: ignore[union-attr]
        except IllegalTransition:
            return
        rec.status = to
        self.on_change()
        if to in T.TERMINAL:
            rec.finished = self.clock()
            rec.done.set()
            self.on_result(rec)

    def _end(self, rec: TaskRecord, state: str, reason: str, **facts) -> None:
        rec.reason = reason
        self._move(rec, state, **facts)
        self.log("task_finished", {"task": rec.id, "node": rec.node_id, "type": rec.task_type, "status": rec.status, "reason": reason[:100], "duration_s": rec.duration_s})

    def start(self, node_id: str, task_type: str, params: dict | None = None, *, by: str = "GUARD", timeout_s: float | None = None) -> TaskRecord:
        """Create and send a task. Returns at once; `await rec.done.wait()` for the end. Everything that can go wrong ends the task with a typed reason, never an exception."""
        tid = secrets.token_hex(6)
        rec = TaskRecord(tid, node_id, task_type, by, self.clock())
        self.tasks[tid] = rec
        self.on_change()
        rec.guard = ProtocolGuard("protocol-task", lambda t, p: self.log(t, p), clock=self.clock, subject=tid)
        self.log("task_created", {"task": tid, "node": node_id, "type": task_type}, owner=(by == "OWNER"))
        try:
            T.validate_params(task_type, params or {})
        except ValueError as exc:
            self._end(rec, "rejected", str(exc), **{"task.rejected": True})
            return rec
        session = self.sessions(node_id)
        if session is None:
            self._end(rec, "failed", "node_offline", **{"task.result": "failed"})
            return rec
        if sum(1 for t in self.tasks.values() if t.node_id == node_id and t.status in ("sent", "accepted", "running")) >= self.max_per_node:
            self._end(rec, "rejected", "busy", **{"task.rejected": True})
            return rec
        timeout = timeout_s or T.TIMEOUTS_S.get(task_type, 60.0)
        token = T.issue_token(session.identity, task_id=tid, node_id=node_id, sid=session.sid, tools=[task_type], ttl_s=min(T.DEFAULT_TTL_S, timeout))
        rec.guard.record("task.created", True)
        self._move(rec, "sent", **{"task.sent": True})
        asyncio.ensure_future(session.send("task", {"task_id": tid, "task_type": task_type, "params": params or {}, "token": token, "timeout_s": timeout}))
        asyncio.ensure_future(self._watch(rec, session, timeout))
        return rec

    async def _watch(self, rec: TaskRecord, session, timeout: float) -> None:
        try:
            await asyncio.wait_for(rec.done.wait(), timeout)
        except TimeoutError:
            if rec.status not in T.TERMINAL:
                self._end(rec, "timed_out", "no result in time", **{"task.timeout": True})
                await session.send("cancel", {"task_id": rec.id})

    async def cancel(self, task_id: str) -> bool:
        rec = self.tasks.get(task_id)
        if rec is None or rec.status in T.TERMINAL:
            return False
        session = self.sessions(rec.node_id)
        if session is not None:
            await session.send("cancel", {"task_id": task_id})
        self._end(rec, "cancelled", "cancelled by the owner", **{"task.cancelled": True})
        return True

    def on_update(self, node_id: str, payload: dict) -> None:
        rec = self.tasks.get(str(payload.get("task_id", "")))
        if rec is None or rec.node_id != node_id or rec.status in T.TERMINAL:
            self.log("task_message_ignored", {"node": node_id, "reason": "unknown or finished task"})
            return
        state = payload.get("state")
        if state == "accepted":
            self._move(rec, "accepted", **{"task.accepted": True})
        elif state == "running":
            self._move(rec, "running", **{"task.running": True})
        elif state == "rejected":
            self._end(rec, "rejected", str(payload.get("reason", ""))[:60], **{"task.accepted": False})

    def on_node_result(self, node_id: str, payload: dict) -> None:
        rec = self.tasks.get(str(payload.get("task_id", "")))
        if rec is None or rec.node_id != node_id or rec.status in T.TERMINAL:
            self.log("task_message_ignored", {"node": node_id, "reason": "unknown or finished task"})
            return
        try:
            res = T.TaskResult.model_validate(payload)
        except Exception:
            self._end(rec, "failed", "the result did not have the agreed form", **{"task.result": "failed"})
            return
        rec.result, rec.evidence, rec.duration_s = res.result, res.evidence, res.duration_s
        if rec.status == "sent":  # a node that answers with the result straight away: still walk the states in order
            self._move(rec, "accepted", **{"task.accepted": True})
        if rec.status == "accepted":
            self._move(rec, "running", **{"task.running": True})
        if res.status == "done":
            self._end(rec, "done", "", **{"task.result": "done"})
        elif res.status == "cancelled":
            self._end(rec, "cancelled", res.reason, **{"task.cancelled": True})
        else:
            self._end(rec, "failed", res.reason, **{"task.result": "failed"})

    def node_gone(self, node_id: str) -> None:
        """The node's connection ended: whatever it was still doing is over, as a failure with a clear reason."""
        for rec in list(self.tasks.values()):
            if rec.node_id == node_id and rec.status not in T.TERMINAL:
                self._end(rec, "failed", "node_disconnected", **{"task.result": "failed"})

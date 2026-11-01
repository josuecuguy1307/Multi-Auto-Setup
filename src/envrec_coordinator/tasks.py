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

from envrec_net import envrun as ER
from envrec_net import setup as P
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
    def __init__(self, *, clock: Callable[[], float] = time.time, log: Callable[..., object], sessions: Callable[[str], object | None], max_per_node: int = MAX_PER_NODE,
                 approvals: Callable[[str, int, str], str | None] = lambda plan_hash, step, action: None,
                 origins: Callable[..., str | None] = lambda task_id, task_type=None, sensitivity=None: None,
                 credential_approvals: Callable[[str, str, str], str | None] = lambda plan_hash, name, action: None) -> None:
        self.clock, self.log, self.sessions, self.max_per_node = clock, log, sessions, max_per_node
        self.approvals = approvals  # (plan hash, step, action) -> the hash of the OWNER event in the log that approved exactly that, or None. The default approves nothing.
        self.credential_approvals = credential_approvals  # (plan hash, credential name, action) -> the hash of the OWNER event in the log that approved exactly that, or None. The default approves nothing.
        self.origins = origins  # (task id, type, level) -> the id of the OWNER event in the log that created or approved that task, or None. The default accepts nothing.
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
        if task_type in T.WRITE_TYPES:  # a write goes through `start_setup_step` and nowhere else: it needs the owner's approval of the exact plan
            self.tasks[tid] = rec
            self.log("task_created", {"task": tid, "node": node_id, "type": task_type}, owner=False)
            rec.guard = ProtocolGuard("protocol-task", lambda t, p: self.log(t, p), clock=self.clock, subject=tid)
            self._end(rec, "rejected", "write_refused", **{"task.rejected": True})
            return rec
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

    def start_setup_step(self, node_id: str, plan: dict, step: int, action: str = "run", *, timeout_s: float | None = None) -> TaskRecord:
        """Send one step of an approved plan to a node. The task is made by CODE (GUARD) and sent only when the log holds an OWNER approval of this exact plan hash, step and action; a plan whose hash is
        not its own, or one nobody approved, ends `rejected` at once and nothing leaves this computer. The approval's event hash is signed into the node's token."""
        tid = secrets.token_hex(6)
        rec = TaskRecord(tid, node_id, "setup_step", "GUARD", self.clock())
        self.tasks[tid] = rec
        self.on_change()
        rec.guard = ProtocolGuard("protocol-task", lambda t, p: self.log(t, p), clock=self.clock, subject=tid)
        self.log("task_created", {"task": tid, "node": node_id, "type": "setup_step", "step": step, "action": action})
        try:
            parsed = P.parse_plan(plan)
            spec = parsed.step(step)
            T.validate_params("setup_step", {"plan": plan, "step": step, "action": action})
        except (P.PlanError, ValueError) as exc:
            self._end(rec, "rejected", "bad_plan" if isinstance(exc, P.PlanError) else str(exc), **{"task.rejected": True})
            return rec
        digest = P.plan_hash(parsed)
        if parsed.plan_hash != digest:
            self._end(rec, "rejected", "plan_altered", **{"task.rejected": True})
            return rec
        approval = self.approvals(digest, step, action)
        if not approval:
            self._end(rec, "rejected", "no_owner_approval", **{"task.rejected": True})
            return rec
        session = self.sessions(node_id)
        if session is None:
            self._end(rec, "failed", "node_offline", **{"task.result": "failed"})
            return rec
        if sum(1 for t in self.tasks.values() if t.node_id == node_id and t.status in ("sent", "accepted", "running")) >= self.max_per_node:
            self._end(rec, "rejected", "busy", **{"task.rejected": True})
            return rec
        timeout = timeout_s or T.TIMEOUTS_S["setup_step"]
        scope = T.write_scope(digest, parsed.recipe_id, step, action, spec.writes, approval)
        token = T.issue_token(session.identity, task_id=tid, node_id=node_id, sid=session.sid, tools=["setup_step"], ttl_s=min(T.DEFAULT_TTL_S, timeout), scope=scope)
        rec.guard.record("task.created", True)
        self._move(rec, "sent", **{"task.sent": True})
        asyncio.ensure_future(session.send("task", {"task_id": tid, "task_type": "setup_step", "params": {"plan": plan, "step": step, "action": action}, "token": token, "timeout_s": timeout}))
        asyncio.ensure_future(self._watch(rec, session, timeout))
        return rec

    def start_credential_step(self, node_id: str, name: str, action: str, plan_hash: str, value: str | None = None, *, timeout_s: float | None = None) -> TaskRecord:
        """Send one credential change to a node: store (`set`, the only task that carries a value), set aside (`remove`) or bring back (`restore`). Made by CODE (GUARD) and sent only when the log holds an
        OWNER approval, given with a human gesture, of exactly this plan hash, name and action; without one the task ends `rejected` at once and nothing leaves this computer. The value goes only into the
        sealed task and is never logged, kept or returned: this record holds no parameters."""
        tid = secrets.token_hex(6)
        rec = TaskRecord(tid, node_id, "credential_step", "GUARD", self.clock())
        self.tasks[tid] = rec
        self.on_change()
        rec.guard = ProtocolGuard("protocol-task", lambda t, p: self.log(t, p), clock=self.clock, subject=tid)
        self.log("task_created", {"task": tid, "node": node_id, "type": "credential_step", "credential": name, "action": action})
        try:
            T.validate_params("credential_step", {"name": name, "action": action, "plan_hash": plan_hash, "value": value})
        except ValueError as exc:
            self._end(rec, "rejected", str(exc), **{"task.rejected": True})
            return rec
        approval = self.credential_approvals(plan_hash, name, action)
        if not approval:
            self._end(rec, "rejected", "no_owner_approval", **{"task.rejected": True})
            return rec
        session = self.sessions(node_id)
        if session is None:
            self._end(rec, "failed", "node_offline", **{"task.result": "failed"})
            return rec
        timeout = timeout_s or T.TIMEOUTS_S["credential_step"]
        token = T.issue_token(session.identity, task_id=tid, node_id=node_id, sid=session.sid, tools=["credential_step"], ttl_s=min(T.DEFAULT_TTL_S, timeout), scope=T.credential_scope(plan_hash, name, action, approval))
        rec.guard.record("task.created", True)
        self._move(rec, "sent", **{"task.sent": True})
        asyncio.ensure_future(session.send("task", {"task_id": tid, "task_type": "credential_step", "params": {"name": name, "action": action, "plan_hash": plan_hash, **({"value": value} if value is not None else {})}, "token": token, "timeout_s": timeout}))
        asyncio.ensure_future(self._watch(rec, session, timeout))
        return rec

    def start_environment_run(self, node_id: str, task_id: str, entry: dict, params: dict, decision: dict, *, timeout_s: float | None = None) -> TaskRecord:
        """Hand a routed task to an environment on a node. Made by CODE (GUARD), and sent only when the log holds an OWNER event that created or approved THIS task (`origins`); with none, the task ends
        `rejected` at once and nothing leaves this computer. The origin's event hash, the environment, its recipe hash, the mode, the workspace and the endpoint are signed into the node's token."""
        tid = secrets.token_hex(6)
        rec = TaskRecord(tid, node_id, "environment_run", "GUARD", self.clock())
        self.tasks[tid] = rec
        self.on_change()
        rec.guard = ProtocolGuard("protocol-task", lambda t, p: self.log(t, p), clock=self.clock, subject=tid)
        self.log("task_created", {"task": tid, "node": node_id, "type": "environment_run", "routed_task": task_id, "environment": entry.get("id")})
        try:
            parsed = T.validate_params("environment_run", params)
        except ValueError as exc:
            self._end(rec, "rejected", str(exc), **{"task.rejected": True})
            return rec
        origin = self.origins(task_id, parsed.task_type, parsed.sensitivity)
        if not origin:
            self._end(rec, "rejected", "no_owner_origin", **{"task.rejected": True})
            return rec
        if parsed.environment_id != entry.get("id") or parsed.recipe_sha256 != ER.recipe_sha256(entry):
            self._end(rec, "rejected", "stale_recipe", **{"task.rejected": True})
            return rec
        session = self.sessions(node_id)
        if session is None:
            self._end(rec, "failed", "node_offline", **{"task.result": "failed"})
            return rec
        if sum(1 for t in self.tasks.values() if t.node_id == node_id and t.status in ("sent", "accepted", "running")) >= self.max_per_node:
            self._end(rec, "rejected", "busy", **{"task.rejected": True})
            return rec
        timeout = timeout_s or T.TIMEOUTS_S["environment_run"]
        scope = ER.env_scope(entry=entry, task_type=parsed.task_type, sensitivity=parsed.sensitivity, workspace_id=parsed.workspace_id, endpoint=parsed.endpoint, origin_event=origin, decision=decision, access_id=parsed.access_id)
        token = T.issue_token(session.identity, task_id=tid, node_id=node_id, sid=session.sid, tools=["environment_run"], ttl_s=min(T.DEFAULT_TTL_S, timeout), scope=scope)
        rec.guard.record("task.created", True)
        self._move(rec, "sent", **{"task.sent": True})
        asyncio.ensure_future(session.send("task", {"task_id": tid, "task_type": "environment_run", "params": params, "token": token, "timeout_s": timeout}))
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

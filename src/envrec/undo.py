"""The undo executor: replay the undo entries a run recorded, newest first, each one a write tool that asks the owner again with its exact action. Nothing is undone silently."""

from __future__ import annotations

from dataclasses import dataclass

from envrec.contracts import Actor, Event
from envrec.tools import get_tool
from envrec.tools.ctx import ToolContext, ToolResult, resolve_refs, run_write_tool


@dataclass
class UndoStep:
    step: str
    undo: str
    ok: bool
    detail: str


def pending_tool_undos(store, target_id: str) -> list[tuple[int, dict]]:
    """Tool-based undo entries of this target that were not applied yet, as (event index, payload), newest first."""
    events = store.events()
    applied = {e.payload.get("entry") for e in events if e.type == "undo_applied" and e.payload.get("target") == target_id}
    out = [(i, e.payload) for i, e in enumerate(events) if e.type == "undo_entry" and e.payload.get("target") == target_id and "text" in e.payload and e.id not in applied]
    return list(reversed(out))


def run_undo(store, target, ctx_factory) -> list[UndoStep]:
    """For each pending entry (newest first): build the call, ask the owner, run it. `ctx_factory()` returns a ToolContext bound to this store and target (approve included)."""
    results: list[UndoStep] = []
    events = store.events()
    for index, payload in pending_tool_undos(store, target.id):
        entry_id = events[index].id
        tool = get_tool(payload["undo"])
        ctx: ToolContext = ctx_factory()
        ctx.undoing = True
        if tool is None:
            results.append(UndoStep(payload["step"], payload["undo"], False, "no such undo tool"))
            continue
        ctx.resolved, err = resolve_refs(tool, payload["values"], store, target)
        res: ToolResult = ToolResult.fail(__import__("envrec.discovery.types", fromlist=["Reason"]).Reason.not_applicable, err) if err else run_write_tool(tool, ctx, payload["values"])
        store.append_event(Event(actor=Actor.loop, type="undo_applied", payload={"target": target.id, "entry": entry_id, "step": payload["step"], "ok": res.ok, "detail": res.detail[:200]}))
        results.append(UndoStep(payload["step"], payload["undo"], res.ok, res.detail or "done"))
    return results

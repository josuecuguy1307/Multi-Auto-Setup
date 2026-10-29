"""What a tool receives (ToolContext) and what it returns (ToolResult), and the one function that runs a tool with every rule enforced."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from envrec.discovery.sanitize import Salts
from envrec.discovery.types import Capture, Reason
from envrec.tools.registry import Tool, ToolArgError


@dataclass
class ToolContext:
    collector: Any  # a discovery Collector: local, remote or replay
    platform: str
    store: Any = None
    target: Any = None
    salts: Salts = field(default_factory=Salts)
    params: dict[str, Any] = field(default_factory=dict)  # run-level settings: seconds, scope, rate, ...
    approve: Callable[[str], bool] | None = None  # the human's approval; read-tier tools refuse to send without it
    event_id: str | None = None
    known_snapshot: dict[str, Any] | None = None  # taken by the caller on its own thread (the store is not shared across threads)
    owner_thread: int = field(default_factory=threading.get_ident)  # the thread that created this context, the only one that may touch the store
    approvals_log: list = field(default_factory=list)  # approvals asked on another thread: the owner thread writes them as events (`flush_approvals`)
    undoing: bool = False  # the undo executor is running this write: it is itself the reversal, so it records no further undo entry
    write_cleared: bool = False  # set by `prepare_write` once the owner said yes and the undo entry is recorded; a write tool refuses to run without it
    resolved: dict[str, Any] = field(default_factory=dict)  # argument name -> what its reference resolved to (`resolve_refs`, done on the owner thread before the tool runs)

    def recording_approve(self, action: str) -> Callable[[str], bool]:
        """An approve function for an approval a tool asks ITSELF, in the middle of its work (run_tool only records the one it asks before running the tool). Same answer as `approve`
        (no approver means no), and every answer, a refusal too, is written as an `approval` event with the exact text the owner was shown."""
        def ask(text: str) -> bool:
            granted = bool(self.approve is not None and self.approve(text))
            if self.store is not None and self.target is not None:
                entry = {"tool": action, "action": action, "target": self.target.id, "tier": "write", "plan": text[:600], "granted": granted}
                if threading.get_ident() == self.owner_thread:
                    _approval_event(self.store, entry)
                else:
                    self.approvals_log.append(entry)  # the store belongs to the owner thread: it flushes this after the tool returns
            return granted
        return ask

    def ref_text(self, arg: str) -> str | None:
        """The text a capture reference resolved to (code read it from the store; the model never saw it), or None when that argument was not resolved."""
        r = self.resolved.get(arg)
        return r.text if r is not None else None

    def ref_fact(self, arg: str):
        """The fact a fact reference resolved to, or None."""
        r = self.resolved.get(arg)
        return r.fact if r is not None else None

    def known(self) -> dict[str, Any]:
        """Fact values already in the store for this target (what a probe may build on)."""
        if self.known_snapshot is not None:
            return self.known_snapshot
        if self.store is None or self.target is None:
            return {}
        return {f.key: f.value for f in self.store.current_facts(self.target.id) if f.status.value != "unknown"}


@dataclass
class ToolResult:
    ok: bool = True
    captures: list[Capture] = field(default_factory=list)  # raw bytes with their exact source: hashed and stored, never shown to the model
    data: Any = None  # structured result a skill's extraction rules read (JSON-able)
    facts: dict[str, Any] | None = None  # a typed result keyed by fact keys (value None = could not be determined)
    provider: str | None = None
    reasons: dict[str, str] = field(default_factory=dict)  # fact key -> why it is None
    limitations: list[str] = field(default_factory=list)
    error: Reason | None = None
    detail: str = ""

    @classmethod
    def fail(cls, reason: Reason, detail: str, captures: list[Capture] | None = None) -> ToolResult:
        return cls(ok=False, captures=captures or [], error=reason, detail=detail)

    @classmethod
    def from_capture(cls, c: Capture) -> ToolResult:
        if c.error is not None:
            return cls(ok=False, captures=[c], error=c.error, detail=c.detail)
        return cls(ok=True, captures=[c])


MAX_REF_CHARS = 200_000


@dataclass
class Resolved:
    """What a reference argument pointed at. `text` for a capture, `fact` for a fact; `ident` is the sha256 or the fact id (for the record)."""

    kind: str
    ident: str
    text: str = ""
    fact: Any = None


def resolve_refs(tool: Tool, args: dict[str, Any], store, target) -> tuple[dict[str, Resolved], str | None]:
    """Resolve every reference argument of a call from the store, for THIS target only. Runs on the thread that owns the store. Returns (resolved, error sentence or None)."""
    out: dict[str, Resolved] = {}
    for a in tool.args:
        if not a.ref or a.name not in args:
            continue
        if store is None or target is None:
            return out, f"{a.name}: there is no store to read the {a.ref} from"
        v = str(args[a.name]).strip()
        if a.ref == "capture":
            from envrec.store.store import RawHashMismatch
            try:
                ref = store.resolve_capture(target.id, v)
                out[a.name] = Resolved("capture", ref.sha256, text=store.read_raw(ref).decode("utf-8", "replace")[:MAX_REF_CHARS])
            except (KeyError, RawHashMismatch, OSError) as exc:
                return out, f"{a.name}: {exc.args[0] if exc.args else exc}"
        else:
            fact = store.current_fact(target.id, v) or store.get_fact(v)
            if fact is None or fact.target_id != target.id:
                return out, f"{a.name}: no fact {v[:60]!r} for this target; the FACTS list shows the ones that exist"
            out[a.name] = Resolved("fact", fact.id, fact=fact)
    return out, None


def prepare_write(tool: Tool, ctx: ToolContext, args: dict[str, Any] | None) -> tuple[bool, str]:
    """Everything that must be true BEFORE a write tool runs, in this order, on the thread that owns the store: the arguments are valid, the owner is shown the exact action AND its
    undo and answers (a refusal is an event too), and the undo entry is on the record. Only then is the tool cleared to run. Returns (cleared, why not)."""
    from envrec.contracts import Actor, Event
    try:
        clean = tool.check_args(dict(args or {}))
    except ToolArgError as exc:
        return False, str(exc)
    try:
        action = tool.approval(ctx, **clean)
        undo = tool.undo(ctx, **clean)  # a write whose undo cannot be stated is not offered to the owner at all
    except ValueError as exc:
        return False, str(exc)
    shown = f"{action}\n  undo: {undo['text']}"
    granted = bool(ctx.approve is not None and ctx.approve(shown))
    if ctx.store is not None and ctx.target is not None:
        _approval_event(ctx.store, {"tool": tool.name, "action": tool.name, "target": ctx.target.id, "tier": "write", "plan": shown[:600], "granted": granted})
        if granted and not ctx.undoing:
            ctx.store.append_event(Event(actor=Actor.loop, type="undo_entry", payload={"target": ctx.target.id, "step": tool.name, "undo": undo["tool"], "values": undo["args"], "text": undo["text"]}))
    if not granted:
        return False, "not approved: " + action[:200]
    ctx.write_cleared = True
    return True, ""


def run_write_tool(tool: Tool, ctx: ToolContext, args: dict[str, Any] | None = None) -> ToolResult:
    """prepare_write, then the tool. For callers that are not the loop (the undo executor, tests)."""
    ok, why = prepare_write(tool, ctx, args)
    if not ok:
        return ToolResult.fail(Reason.denied, why)
    try:
        return run_tool(tool, ctx, args)
    finally:
        ctx.write_cleared = False


def _approval_event(store, entry: dict) -> None:
    from envrec.contracts import Actor, Event
    store.append_event(Event(actor=Actor.human, type="approval", payload=entry))


def flush_approvals(ctx: ToolContext) -> None:
    """Write, on the thread that owns the store, the approvals that were asked on a worker thread."""
    while ctx.approvals_log:
        _approval_event(ctx.store, ctx.approvals_log.pop(0))


def run_tool(tool: Tool, ctx: ToolContext, args: dict[str, Any] | None = None) -> ToolResult:
    """Validate the arguments, check platform and tier, ask for approval when the tool is read-tier, run it, and contain any failure."""
    if not tool.applies(ctx.platform):
        return ToolResult.fail(Reason.not_applicable, f"{tool.name} does not support {ctx.platform}")
    try:
        clean = tool.check_args(dict(args or {}))
    except ToolArgError as exc:
        return ToolResult.fail(Reason.denied, str(exc))
    if tool.tier == "write" and not ctx.write_cleared:
        return ToolResult.fail(Reason.denied, f"{tool.name} is a write tool: it runs only after the owner approved the exact action and its undo was recorded")
    if tool.tier == "read" and getattr(ctx.collector, "origin", "") != "replay":  # a recording sends nothing, so it needs no approval
        text = tool.approval(ctx, **clean) if tool.approval else f"run the read tool {tool.name} with {clean}"
        granted = bool(ctx.approve is not None and ctx.approve(text))
        if ctx.store is not None and ctx.target is not None:  # every approval is on the record, a refusal too
            entry = {"tool": tool.name, "target": ctx.target.id, "tier": tool.tier, "plan": text[:400], "granted": granted}
            if threading.get_ident() == ctx.owner_thread:
                _approval_event(ctx.store, entry)
            else:
                ctx.approvals_log.append(entry)  # the store belongs to the owner thread: it flushes this after the tool returns
        if not granted:
            return ToolResult.fail(Reason.denied, "not approved: " + text[:200])
    try:
        res = tool.fn(ctx, **clean)
    except Exception as exc:  # a tool never takes the run down
        return ToolResult.fail(Reason.unavailable, f"{type(exc).__name__}: {str(exc)[:160]}")
    if not isinstance(res, ToolResult):
        return ToolResult.fail(Reason.unavailable, f"{tool.name} returned {type(res).__name__}, not a ToolResult")
    return res


async def run_tool_async(tool: Tool, ctx: ToolContext, args: dict[str, Any] | None = None) -> ToolResult:
    return await asyncio.to_thread(run_tool, tool, ctx, args)

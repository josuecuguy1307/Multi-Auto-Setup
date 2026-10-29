"""What a tool receives (ToolContext) and what it returns (ToolResult), and the one function that runs a tool with every rule enforced."""

from __future__ import annotations

import asyncio
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


def run_tool(tool: Tool, ctx: ToolContext, args: dict[str, Any] | None = None) -> ToolResult:
    """Validate the arguments, check platform and tier, ask for approval when the tool is read-tier, run it, and contain any failure."""
    if not tool.applies(ctx.platform):
        return ToolResult.fail(Reason.not_applicable, f"{tool.name} does not support {ctx.platform}")
    try:
        clean = tool.check_args(dict(args or {}))
    except ToolArgError as exc:
        return ToolResult.fail(Reason.denied, str(exc))
    if tool.tier == "read" and getattr(ctx.collector, "origin", "") != "replay":  # a recording sends nothing, so it needs no approval
        text = tool.approval(ctx, **clean) if tool.approval else f"run the read tool {tool.name} with {clean}"
        if ctx.approve is None or not ctx.approve(text):
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

"""Run a skill's plan: registered tools collect captures, the sandboxed script turns them into typed output, the privacy gate stores it.

The script never runs anything. When it needs another read it asks (`next`), and every asked call is checked here: the tool must be registered, its tier within the skill's,
its arguments valid, its reducer one the gate knows. Output that cites no capture of this run is rejected, and for a generated skill every observed value must be
quoted from the capture it cites. The store is only ever written through the PrivacyGate, from this thread of control."""

from __future__ import annotations

import dataclasses
import json
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any

from envrec.discovery.runner import AdapterRun, _finish
from envrec.discovery.sanitize import Salts
from envrec.discovery.types import Capture, Entity, FactOut, Reason, UnknownOut
from envrec.privacy.gate import PrivacyGate
from envrec.privacy.reducers import CAPTURE_REDUCERS, reduce_capture_bytes
from envrec.skills.sbx import SandboxRefused, SandboxUnavailable, ScriptFailed, run_script
from envrec.skills.skillmd import Skill
from envrec.tools.ctx import ToolContext, ToolResult, run_tool
from envrec.tools.registry import ToolArgError, get_tool

SKILL_TIMEOUT_S = 120.0
MAX_CALLS_TOTAL = 160
TIERS = {"observe": 0, "read": 1}


class SkillRejected(ValueError):
    """Output or a request that breaks a rule. The message names the rule."""


@dataclass(frozen=True)
class _Info:
    id: str
    ring: int


@dataclass
class Collected:
    caps: dict[str, Capture] = field(default_factory=dict)
    reducers: dict[str, str | None] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    out: dict = field(default_factory=lambda: {"facts": [], "entities": [], "unknowns": [], "next": []})
    rounds: int = 0
    tools_run: list[str] = field(default_factory=list)


class _Relay:
    """Tools run in a worker thread but approval questions must be asked from the calling thread (the store is bound to it). Waiting for the human does not count
    against the timeout."""

    def __init__(self, approve) -> None:
        self.approve, self.q = approve, queue.Queue()

    def ask(self, text: str) -> bool:
        box = {"event": threading.Event(), "yes": False}
        self.q.put((text, box))
        box["event"].wait()
        return box["yes"]

    def pump(self, fut, timeout: float):
        deadline = time.monotonic() + timeout
        while True:
            try:
                text, box = self.q.get(timeout=0.05)
            except queue.Empty:
                if fut.done():
                    return fut.result()
                if time.monotonic() > deadline:
                    raise FutureTimeout
                continue
            t_ask = time.monotonic()
            box["yes"] = bool(self.approve(text))
            box["event"].set()
            deadline += time.monotonic() - t_ask


# ---- rendering and one call --------------------------------------------------------------------------------------------------------

def render_args(args: Any, params: dict[str, Any]) -> Any:
    if isinstance(args, dict):
        return {k: v2 for k, v in args.items() if (v2 := render_args(v, params)) is not None}  # a parameter nobody gave is not passed: the tool's default applies
    if isinstance(args, list):
        return [render_args(v, params) for v in args]
    if isinstance(args, str) and args.startswith("{param.") and args.endswith("}"):
        return params.get(args[7:-1])
    return args


def applies(platforms: list[str], platform: str) -> bool:
    return "any" in platforms or platform in platforms


def check_call(skill: Skill, tool_name: str, args: dict, reducer: str | None) -> Any:
    t = get_tool(tool_name)
    if t is None:
        raise SkillRejected(f"the tool {tool_name!r} is not registered: a skill can use only registered tools")
    if TIERS[t.tier] > TIERS[skill.meta.tier]:
        raise SkillRejected(f"the tool {tool_name!r} is {t.tier}-tier but the skill is {skill.meta.tier}-tier")
    if reducer is not None and reducer not in CAPTURE_REDUCERS:
        raise SkillRejected(f"unknown privacy reducer {reducer!r}")
    try:
        t.check_args(dict(args))
    except ToolArgError as exc:
        raise SkillRejected(str(exc)) from None
    return t


def call_tool(ctx: ToolContext, call_id: str, tool_name: str, args: dict, reducer: str | None) -> tuple[list[Capture], str | None]:
    """Run one registered tool. Returns its captures (always at least one, named by the call id when the tool made none) and the reducer for them."""
    t = get_tool(tool_name)
    args = dict(args)
    if any(a.name == "name" for a in t.args) and "name" not in args:
        args["name"] = call_id
    res: ToolResult = run_tool(t, ctx, args)
    caps = list(res.captures)
    if not res.ok:
        if not caps:
            caps = [Capture(call_id, f"tool:{tool_name}", b"", None, getattr(ctx.collector, "origin", "local"), ctx.platform, res.error or Reason.unavailable, res.detail)]
        elif all(c.error is None for c in caps):  # the tool failed after collecting: say so on the first capture
            c = caps[0]
            caps[0] = dataclasses.replace(c, error=res.error or Reason.unavailable, detail=res.detail)
    elif res.data is not None and not caps:
        caps = [Capture(call_id, f"tool:{tool_name}", json.dumps(res.data, default=str).encode(), 0, getattr(ctx.collector, "origin", "local"), ctx.platform)]
    elif not caps:
        caps = [Capture(call_id, f"tool:{tool_name}", b"", 0, getattr(ctx.collector, "origin", "local"), ctx.platform)]
    return caps, reducer


def script_input(skill: Skill, ctx: ToolContext, params: dict, col: Collected, rnd: int) -> dict:
    return {"platform": ctx.platform, "params": {k: v for k, v in params.items() if isinstance(v, (str, int, float, bool))}, "round": rnd,
            "captures": {n: {"text": c.text, "ok": c.ok, "error": c.error.value if c.error else None, "detail": c.detail} for n, c in col.caps.items()}}


def _run_calls(skill: Skill, ctx: ToolContext, calls: list[tuple[str, str, dict, str | None]], col: Collected) -> None:
    def one(c):
        cid, tool, args, reducer = c
        return call_tool(ctx, cid, tool, args, reducer), tool
    with ThreadPoolExecutor(max_workers=16) as ex:
        for (caps, reducer), tool in ex.map(one, calls):
            col.tools_run.append(tool)
            for c in caps:
                if reducer and c.data:  # reduced BEFORE anything else sees it: the script and the store only ever get names, never tokens
                    c = dataclasses.replace(c, data=reduce_capture_bytes(c.data, reducer))
                col.caps[c.name] = c
                col.reducers[c.name] = reducer


def collect_and_parse(skill: Skill, ctx: ToolContext, params: dict, script_runner=run_script, preloaded: dict[str, Capture] | None = None) -> Collected:
    """`preloaded` replays recorded captures instead of calling tools (the skill's own tests): nothing is collected, reads the script asks for that were not recorded
    are unavailable."""
    plan = skill.meta.plan
    col = Collected()
    if plan is None:
        return col
    if preloaded is not None:
        col.caps = dict(preloaded)
    steps = []
    for s in plan.steps:
        if not applies(s.platforms, ctx.platform):
            continue
        try:
            check_call(skill, s.tool, s.args, s.reducer)
        except SkillRejected as exc:
            col.errors.append(f"{s.id}: {exc}")
            continue
        steps.append((s.id, s.tool, render_args(s.args, params), s.reducer))
    if preloaded is None:
        _run_calls(skill, ctx, steps, col)
    if plan.script is None:
        return col
    total = len(steps)
    for rnd in range(plan.max_rounds):
        col.rounds = rnd + 1
        col.out = script_runner(skill.path, plan.script, script_input(skill, ctx, params, col, rnd), timeout_s=plan.timeout_s)
        asked = [c for c in col.out["next"] if c["id"] not in col.caps]
        if not asked:
            break
        calls = []
        for c in asked:
            try:
                check_call(skill, c["tool"], c.get("args", {}), c.get("reducer"))
            except SkillRejected as exc:
                col.errors.append(f"requested call {c['id']}: {exc}")
                col.caps[c["id"]] = Capture(c["id"], f"tool:{c['tool']}", b"", None, "local", ctx.platform, Reason.denied, str(exc)[:160])
                continue
            calls.append((c["id"], c["tool"], c.get("args", {}), c.get("reducer")))
        total += len(calls)
        if total > MAX_CALLS_TOTAL:
            raise SkillRejected(f"the script asked for more than {MAX_CALLS_TOTAL} tool calls")
        if preloaded is not None:
            for cid, *_ in calls:
                col.caps[cid] = Capture(cid, "replay", b"", None, "replay", ctx.platform, Reason.unavailable, "not recorded")
        else:
            _run_calls(skill, ctx, calls, col)
    else:
        col.errors.append(f"the script was still asking for reads after {plan.max_rounds} rounds")
    return col


# ---- the evidence rule and the conversion to typed outputs -----------------------------------------------------------------------------

def _norm(s: str) -> str:
    return " ".join(str(s).split())


def to_outs(skill: Skill, col: Collected) -> list:
    """Typed outputs from the script's final answer. Anything that cites no capture of this run is rejected (the whole answer: a skill that lies once is not trusted
    for the rest). A generated skill must also quote, from the capture it cites, the text each observed value came from."""
    generated = skill.meta.provenance == "generated"
    outs: list = []

    def cite(kind: str, label: str, src: str | None, quote: str | None, hypothesis: bool) -> str:
        if not src or src not in col.caps or not col.caps[src].ok:
            raise SkillRejected(f"{kind} {label} cites no usable capture of this run (src={src!r}): evidence is required")
        if generated and not hypothesis:
            if not quote:
                raise SkillRejected(f"{kind} {label} has no `quote`: a generated skill must quote the capture text the value came from")
            if _norm(quote) not in _norm(col.caps[src].text):
                raise SkillRejected(f"{kind} {label}: the quote is not in capture {src!r}; facts that were never in the capture are rejected")
        return src

    for f in col.out["facts"]:
        src = cite("fact", f["key"], f.get("src"), f.get("quote"), bool(f.get("hypothesis")))
        outs.append(FactOut(f["key"], f.get("value"), float(f.get("confidence", 0.9)), [str(x) for x in f.get("limitations", [])], src, bool(f.get("hypothesis"))))
    for e in col.out["entities"]:
        src = cite("entity", e["kind"], e.get("src"), e.get("quote"), bool(e.get("hypothesis")))
        outs.append(Entity(e["kind"], int(e.get("ring", skill.meta.ring)), e["ident"], e.get("owned", "unknown"), dict(e.get("attrs", {})), resolved=bool(e.get("resolved", True)),
                           prefix=e.get("prefix"), src=src, hypothesis=bool(e.get("hypothesis")), confidence=float(e.get("confidence", 0.9)),
                           links=[(str(a), str(b)) for a, b in e.get("links", [])]))
    for u in col.out["unknowns"]:
        try:
            reason = Reason(u.get("reason", "unavailable"))
        except ValueError:
            reason = Reason.unavailable
        outs.append(UnknownOut(u["key"], reason, str(u.get("detail", ""))[:200]))
    return outs


# ---- the whole run ----------------------------------------------------------------------------------------------------------------

def _safely(skill, ctx, params, script_runner) -> tuple[Collected | None, str, Reason | None]:
    try:
        return collect_and_parse(skill, ctx, params, script_runner), "", None
    except SandboxUnavailable as exc:
        return None, str(exc), Reason.unavailable
    except ScriptFailed as exc:
        return None, f"parser failed: {exc}", "error"
    except (SandboxRefused, SkillRejected) as exc:
        return None, f"{type(exc).__name__}: {exc}", Reason.denied
    except Exception as exc:  # a skill never takes the run down
        return None, f"{type(exc).__name__}: {str(exc)[:120]}", Reason.unavailable


def run_skill(skill: Skill, ctx: ToolContext, *, params: dict[str, Any] | None = None, script_runner=run_script, write: bool = True, verified_here: bool = True,
              timeout_s: float = SKILL_TIMEOUT_S) -> AdapterRun:
    t0 = time.monotonic()
    store, target, salts = ctx.store, ctx.target, ctx.salts or Salts()
    info = _Info(skill.id, skill.meta.ring)
    run = AdapterRun(skill.id, skill.meta.ring, "ran")
    run.built = []  # type: ignore[attr-defined]
    run.tools_run = []  # type: ignore[attr-defined]
    if not applies(skill.meta.platforms, ctx.platform):
        run.status, run.reason = "not_applicable", f"{skill.name} has no support for {ctx.platform}"
        return _finish(info, store, target, run, t0)
    p = {**(params or {})}
    ex = ThreadPoolExecutor(max_workers=1)
    relay = _Relay(ctx.approve) if ctx.approve is not None else None
    wctx = dataclasses.replace(ctx, approve=relay.ask) if relay else ctx
    try:
        fut = ex.submit(_safely, skill, wctx, p, script_runner)
        col, crash, why = relay.pump(fut, timeout_s) if relay else fut.result(timeout=timeout_s)
    except FutureTimeout:
        run.status, run.reason = "timeout", f"no result in {timeout_s:g} s"
        return _finish(info, store, target, run, t0)
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    if col is None:
        run.status, run.reason = getattr(why, "value", why), crash
        return _finish(info, store, target, run, t0)
    run.tools_run = col.tools_run
    run.collected = col  # type: ignore[attr-defined]
    caps = list(col.caps.values())
    run.captures = len(caps)
    try:
        outs = to_outs(skill, col)
    except SkillRejected as exc:
        run.status, run.reason = "denied", f"output rejected: {exc}"
        return _finish(info, store, target, run, t0)
    good = [c for c in caps if c.error is None]
    produced = [o for o in outs if isinstance(o, (FactOut, Entity))]
    if not good and not produced:
        reasons = [c.error for c in caps if c.error is not None]
        pick = Reason.permission if Reason.permission in reasons else (reasons[0] if reasons else Reason.unavailable)
        first = next((c for c in caps if c.error is not None), None)
        run.status, run.reason = pick.value, (first.detail if first else "; ".join(col.errors)[:150] or "no capture")
        keys = list(skill.meta.produces)
        run.unknown_reasons = {k: f"{pick.value}: {run.reason}" for k in keys}
        built, _ = PrivacyGate(store, target, salts, skill.key, event_id=ctx.event_id).build([UnknownOut(k, pick, run.reason) for k in keys])
        for f in built:
            store.write_fact(f)
        run.unknowns = len(built)
        return _finish(info, store, target, run, t0)
    gate = PrivacyGate(store, target, salts, skill.key, event_id=ctx.event_id, reducers=col.reducers)
    gate.store_captures(caps)
    if not verified_here:  # a skill that has not passed on THIS device yet cannot assert anything: its output stays a hypothesis
        outs = [FactOut(o.key, o.value, min(o.confidence, 0.5), [*o.limitations, "unverified_here: this skill has not passed its tests on this device"], o.src, True)
                if isinstance(o, FactOut) else o for o in outs]
        for o in outs:
            if isinstance(o, Entity):
                o.hypothesis = True
    built, summary = gate.build(outs)
    if write:
        for f in built:
            store.write_fact(f)
    run.built = built  # type: ignore[attr-defined]
    run.facts, run.entities, run.resolved, run.unknowns = summary.facts, summary.entities, summary.resolved, summary.unknowns
    run.unknown_reasons.update(summary.unknown_reasons)
    notes = list(summary.dropped[:3]) + col.errors[:3]
    if notes:
        run.reason = " | ".join(notes)
    failed = [c for c in caps if c.error is not None]
    if failed:
        run.reason = (run.reason + f" | {len(failed)} of {len(caps)} captures failed: " + "; ".join(f"{c.name}={c.error.value}" for c in failed[:4])).strip(" |")
    return _finish(info, store, target, run, t0)

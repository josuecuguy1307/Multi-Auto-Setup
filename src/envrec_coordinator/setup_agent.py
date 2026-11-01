"""The tools of protocol-setup (docs/NETWORK_CONTRACT.md 3.9): a MODEL picks one action per turn from a small closed menu; CODE builds the plan, asks the OWNER to approve each step, sends it and records what came back.

What the model decides: which action comes next (read the context, re-check the fit, build the plan, run step N, undo step N, finish). What it cannot do: write a plan, a command or a path (code makes the plan from the
closed recipe and the machine's measured facts), approve anything (only the OWNER's answer to the question the tool puts, bound to the plan's hash, is an approval) or run steps out of order (the guard refuses).

The facts it leaves are evidence: `fit.rechecked` and `plan.*` are tested, `setup.minimal_call` is the tested record of the one short question put to the local server (its answer and its time), `setup.verified`
and `setup.undone_all` are what the protocol's exits read."""

from __future__ import annotations

import asyncio
from typing import Any

from envrec.adapters import ToolContext
from envrec.contracts import Actor, CreatedBy, Event, Fact, FactStatus, ProbeResult, TargetKind, ToolSpec
from envrec_coordinator import agent as A
from envrec_coordinator import options as O
from envrec_coordinator.setup_plan import PlanRefused, build_plan
from envrec_net import setup as P
from envrec_net import task as T

APPROVE = [{"id": "approve", "label": "Approve this step", "detail": "the machine runs exactly what is shown here and nothing else"},
           {"id": "reject", "label": "Do not run it", "detail": "nothing is changed"}]


def _code_of(reason: str) -> str:
    """The typed code in a failed step's reason: `StepFailed: checksum_mismatch: ...` -> checksum_mismatch."""
    parts = [p.strip() for p in str(reason).split(":")]
    return next((p for p in parts[1:] if p.replace("_", "").isalnum() and p.islower()), parts[0] if parts else "failed")


class SetupAdapter(A.BaseAdapter):
    def __init__(self, *, node_id: str, node_name: str, load_stored, load_pick, run_task, start_step, approve, owner_events, catalog: dict | None = None, recipes: dict | None = None, rules=None) -> None:
        super().__init__(owner_events=owner_events)
        self.node_id, self.node_name = node_id, node_name
        self.load_stored, self.load_pick = load_stored, load_pick  # () -> the stored analysis / the owner's recorded pick, or None
        self.run_task, self.start_step, self.approve = run_task, start_step, approve  # run_task(type, params) -> record; start_step(plan, n, action) -> record; approve(plan_hash, n, action, by) writes the OWNER event
        self.catalog = catalog if catalog is not None else O.load_catalog()
        self.recipes = recipes
        self.rules = rules or O.load_rules()
        self.pick: dict | None = None
        self.facts: dict[str, Any] = {}  # the machine's facts: the stored analysis, then what the re-check read now
        self.plan: dict | None = None
        self.status: dict[int, str] = {}  # step number -> pending | done | undone | failed
        self.actions = 0  # the ledger: ONLY a step that was run or undone for real (approved by the owner, bound to the plan hash) leaves its own fact (setup.action.K). That is the only progress this
        # protocol makes by acting: a repeated read, a refused or invalid action or a failed result adds nothing, so the loop's no-progress stop still trips

    # ---- the menu -------------------------------------------------------------------------------------------------------------------------------

    def tool_specs(self) -> list[ToolSpec]:
        k = [TargetKind.install]
        by = {
            "load_context": A._spec("load_context", "Read the stored analysis and the owner's pick.", kinds=k, produces=["context.loaded"]),
            "recheck_fit": A._spec("recheck_fit", "Ask the machine for its memory now.", kinds=k, requires=["context.loaded"], produces=["fit.rechecked"], repeatable=True),
            "build_plan": A._spec("build_plan", "Make the setup plan from the recipe.", kinds=k, requires=["fit.rechecked"], produces=["plan.built", "setup.refused"], repeatable=True),
            "run_step": A._spec("run_step", "Run one plan step once the owner approves.", kinds=k, requires=["plan.built"], repeatable=True, schema=A._schema(step=self._hint("run"))),
            "undo_step": A._spec("undo_step", "Undo the latest done step, owner approving.", kinds=k, requires=["plan.built"], repeatable=True, schema=A._schema(step=self._hint("undo"))),
            "finish_setup": A._spec("finish_setup", "End the setup: verified, or all undone.", kinds=k, produces=["setup.finished"], schema=A._schema(summary="what happened, in a sentence")),
        }
        return [by[n] for n in ("load_context", "recheck_fit", "build_plan", "run_step", "undo_step", "finish_setup")]

    def _hint(self, action: str) -> str:
        if not self.plan:
            return "a step number of the plan"
        if action == "run":
            nxt = next((n for n in sorted(self.status) if self.status[n] != "done"), None)
            return f"the next step to run is {nxt}" if nxt else "every step is done"
        done = [n for n, s in self.status.items() if s == "done"]
        return f"the latest done step is {max(done)}" if done else "nothing is done yet"

    def plan_view(self) -> dict | None:
        """The whole plan as data, for a screen: every step with its command, what it writes, its size, its undo and where it stands. Available as soon as the plan is built, before the first approval."""
        if not self.plan:
            return None
        pl = self.plan
        steps = [{"n": s["n"], "op": s["op"], "kind": s["kind"], "title": s["title"], "command": s["exact_command"], "writes": s["writes"], "bytes": s["bytes"], "size_kind": s.get("size_kind"), "undo": s["undo"],
                  "needs_owner_approval": s["needs_owner_approval"], "status": self.status.get(s["n"], "pending")} for s in pl["steps"]]
        return {"plan_title": pl.get("plan_title"), "notes": pl.get("notes", []), "option_title": pl.get("option_title"), "hash": pl["plan_hash"], "plan_id": pl["plan_id"], "machine": pl["machine_id"], "option": pl["option_id"], "sensitivity": pl["sensitivity"], "recipe": pl["recipe_id"], "fit": pl["fit"],
                "total_download_bytes": sum(x["bytes"] for x in steps if x["size_kind"] != "move"), "total_moved_bytes": sum(x["bytes"] for x in steps if x["size_kind"] == "move"), "steps": steps, "verification": pl["verification"]}

    def _event(self, ctx: ToolContext, type: str, payload: dict) -> None:
        """A progress event in the run's own log (tag GUARD: code recorded it), for whoever watches the run."""
        ctx.store.append_event(Event(actor=Actor.loop, type=type, payload=payload, run_id=self.run_id, tag="GUARD"))

    async def _watch_step(self, ctx: ToolContext, rec, n: int, action: str) -> None:
        """Wait for the node's task and say, as it happens, each state it passes through (sent, accepted, running, then how it ended)."""
        last = None
        while not rec.done.is_set():
            if rec.status != last:
                last = rec.status
                self._event(ctx, "setup_step", {"step": n, "action": action, "state": rec.status})
            try:
                await asyncio.wait_for(rec.done.wait(), 0.15)
            except TimeoutError:
                pass
        self._event(ctx, "setup_step", {"step": n, "action": action, "state": rec.status, **({"reason": _code_of(rec.reason)} if rec.status != "done" else {"duration_s": rec.duration_s})})

    # ---- run ----------------------------------------------------------------------------------------------------------------------------------

    async def run(self, tool: str, args: dict[str, Any], ctx: ToolContext) -> ProbeResult:
        handler = {"load_context": self._load, "recheck_fit": self._recheck, "build_plan": self._build, "run_step": self._run_step, "undo_step": self._undo_step, "finish_setup": self._finish}.get(tool)
        if handler is None:
            return ProbeResult(ok=False, error=f"unknown tool {tool}")
        try:
            return await handler(args, ctx)
        except Exception as exc:  # a tool never takes the run down
            return ProbeResult(ok=False, error=f"{type(exc).__name__}: {str(exc)[:200]}")

    def _fact(self, ctx: ToolContext, key: str, value: Any, tool: str, *, status: FactStatus = FactStatus.tested, by: CreatedBy = CreatedBy.loop, confidence: float = 1.0) -> Fact:
        return A._fact(ctx.target.id, key, value, tool, "envrec_coordinator.setup_agent", status=status, created_by=by, event_id=ctx.event_id, confidence=confidence)

    async def _load(self, args: dict, ctx: ToolContext) -> ProbeResult:
        stored, pick = self.load_stored(), self.load_pick()
        if not stored:
            return ProbeResult(ok=False, error="no stored analysis for this machine: analyse it first")
        if not pick:
            return ProbeResult(ok=False, error="the owner has not picked an option for this machine: choose one in the options run first")
        self.pick, self.facts = pick["option"], {k: row["value"] for k, row in stored["facts"].items()}
        out = [A._fact(ctx.target.id, k, v, "load_context", f"stored analysis {stored['run']}", event_id=ctx.event_id, confidence=0.9, limitations=["read from the stored analysis, not probed again"]) for k, v in sorted(self.facts.items())
               if k.startswith("machine.") and not isinstance(v, (list, dict))]
        out.append(self._fact(ctx, "context.loaded", {"analysis_run": stored["run"], "option": self.pick["id"], "title": self.pick.get("title"), "picked_at": pick.get("picked_at")}, "load_context"))
        return ProbeResult(ok=True, facts=out)

    async def _recheck(self, args: dict, ctx: ToolContext) -> ProbeResult:
        rec = self.run_task("memory_for_models", {})
        await rec.done.wait()
        if rec.status != "done":
            return ProbeResult(ok=False, error=f"the machine did not answer: {T.REJECTIONS.get(rec.reason, rec.reason) or rec.status}"[:300])
        self.facts.update(rec.result["facts"])
        n = O._norm(self.facts)
        budget, basis = O.R.memory_budget(O._machine(n), self.rules)
        row = next((r for r in self.catalog.get("model_files", []) if r.get("id") == self.pick.get("model_id")), None)
        if row is None:
            return ProbeResult(ok=False, error="the picked model is not in the catalog any more: build the options again")
        est = O.R.estimate_memory(row, self.rules, context=self.rules.param("default_context"))
        fits = est["total_gib"] <= budget
        value = {"fits": fits, "needs_gib": est["total_gib"], "estimate_label": est["label"], "budget_gib": budget, "basis": basis, "was_needed_gib": self.pick.get("memory_needed_gib"), "available_now_gib": self.facts.get("machine.ram_available_gib")}
        facts = [A._fact(ctx.target.id, k, v, "recheck_fit", f"node:{self.node_name}", event_id=ctx.event_id) for k, v in rec.result["facts"].items() if not isinstance(v, (list, dict))]
        return ProbeResult(ok=True, facts=facts + [self._fact(ctx, "fit.rechecked", value, "recheck_fit")])

    async def _build(self, args: dict, ctx: ToolContext) -> ProbeResult:
        if self.pick is None:
            return ProbeResult(ok=False, error="read the context first")
        from envrec_coordinator.setup_plan import load_recipes
        try:
            plan = build_plan(self.pick, self.facts, self.catalog, self.recipes if self.recipes is not None else load_recipes(), self.rules, machine_id=self.node_id)
        except PlanRefused as exc:  # the protocol's exit `cannot_install` reads this fact; code said no, with a typed reason
            return ProbeResult(ok=True, facts=[self._fact(ctx, "setup.refused", {"reason": exc.reason, "text": exc.text}, "build_plan")])
        self.plan = plan.model_dump(mode="json")
        self.status = {s["n"]: "pending" for s in self.plan["steps"]}
        self._event(ctx, "setup_plan", {"plan_hash": plan.plan_hash, "steps": len(self.plan["steps"]), "download_bytes": sum(x["bytes"] for x in self.plan["steps"])})
        summary = [{"n": s["n"], "op": s["op"], "title": s["title"]} for s in self.plan["steps"]]
        return ProbeResult(ok=True, facts=[self._fact(ctx, "plan.built", True, "build_plan"), self._fact(ctx, "plan.hash", plan.plan_hash, "build_plan"), self._fact(ctx, "plan.steps", summary, "build_plan")])

    # ---- one step, once the owner approves it -----------------------------------------------------------------------------------------------------

    def _ledger(self, ctx: ToolContext, tool: str, n: int, action: str, outcome: str, **extra: Any) -> Fact:
        self.actions += 1
        return self._fact(ctx, f"setup.action.{self.actions}", {"tool": tool, "step": n, "action": action, "outcome": outcome, "plan_hash": self.plan["plan_hash"][:19], **extra}, tool)  # type: ignore[index]

    def _step_number(self, args: dict) -> tuple[int | None, str]:
        try:
            n = int(str(args.get("step", "")).strip().split()[0].rstrip("."))
        except (ValueError, IndexError):
            return None, "name the step by its number"
        if not self.plan or n not in self.status:
            return None, f"the plan has no step {n}"
        return n, ""

    async def _approved(self, ctx: ToolContext, n: int, action: str) -> tuple[bool, str]:
        step = self.plan["steps"][n - 1]  # type: ignore[index]
        what = step["exact_command"] if action == "run" else step["undo"]
        text = (f"{'Run' if action == 'run' else 'Undo'} step {n} of {len(self.plan['steps'])}: {step['title']}\n{'Command' if action == 'run' else 'To undo'}: {what}\n"  # type: ignore[index]
                f"Writes: {', '.join(step['writes']) or 'nothing'}\nUndo: {step['undo']}\nPlan {self.plan['plan_hash'][:19]}…")  # type: ignore[index]
        if self.pending is not None:
            return False, "a question is already waiting for the owner"
        data = {"kind": "approve_step", "plan_hash": self.plan["plan_hash"], "step": n, "of": len(self.plan["steps"]), "action": action, "title": step["title"], "command": step["exact_command"],  # type: ignore[index]
                "writes": step["writes"], "bytes": step["bytes"], "size_kind": step.get("size_kind"), "undo": step["undo"]}  # type: ignore[index]
        self._event(ctx, "setup_step", {"step": n, "action": action, "state": "waiting_for_owner"})
        answer = await self._ask(ctx, f"approve_{action}_{n}", text, APPROVE, data)
        if answer is None:
            return False, "the owner did not answer in time"
        by = self.last_answered_by
        self._owner(ctx.store, "owner_answer", {"question": f"approve_{action}_{n}", "option": answer, "plan_hash": self.plan["plan_hash"], **({"by": by} if by != "owner" else {})})  # type: ignore[index]
        if answer != "approve":
            return False, "the owner did not approve it; nothing was changed"
        self.approve(self.plan["plan_hash"], n, action, by)  # type: ignore[index]  # the OWNER event the coordinator's task desk looks for (an operator's answer is written as the operator's and does not count)
        return True, ""

    async def _run_step(self, args: dict, ctx: ToolContext) -> ProbeResult:
        n, why = self._step_number(args)
        if n is None:
            return ProbeResult(ok=False, error=why)
        if self.status[n] == "done":
            return ProbeResult(ok=False, error=f"step {n} is already done")
        if any(self.status[i] != "done" for i in range(1, n)):
            return ProbeResult(ok=False, error=f"steps run in order: step {next(i for i in range(1, n) if self.status[i] != 'done')} is not done yet")
        ok, why = await self._approved(ctx, n, "run")
        if not ok:
            return ProbeResult(ok=False, error=why)
        self._event(ctx, "setup_step", {"step": n, "action": "run", "state": "approved"})
        rec = self.start_step(self.plan, n, "run")
        await self._watch_step(ctx, rec, n, "run")
        step = self.plan["steps"][n - 1]  # type: ignore[index]
        if rec.status != "done":
            self.status[n] = "failed"
            code = _code_of(rec.reason)
            return ProbeResult(ok=False, facts=[self._fact(ctx, f"step.{n}", {"op": step["op"], "status": "failed", "reason": code, "detail": T.REJECTIONS.get(rec.reason, rec.reason)[:200]}, "run_step")],
                               error=f"step {n} did not succeed: {code}")
        self.status[n] = "done"
        res = rec.result
        out = [self._fact(ctx, f"step.{n}", {"op": step["op"], "status": "done", "duration_s": rec.duration_s}, "run_step"), self._ledger(ctx, "run_step", n, "run", "done")]
        out += self._after_step(step, res, ctx)
        out.append(self._fact(ctx, "setup.undone_all", False, "run_step"))  # something is installed again: "everything was undone" is no longer true
        return ProbeResult(ok=True, facts=out)

    def _after_step(self, step: dict, res: dict, ctx: ToolContext) -> list[Fact]:
        """The facts a finished step leaves besides its own record: the minimal call's numbers and the verification, the server's endpoint."""
        out: list[Fact] = []
        if step["op"] == "minimal_call":
            out += [self._fact(ctx, "setup.minimal_call", {k: res.get(k) for k in ("ok", "tested", "vision", "endpoint", "latency_ms", "answer", "tokens", "tokens_per_s", "resident_gib")}, "run_step"),
                    self._fact(ctx, "setup.verified", bool(res.get("ok")), "run_step")]
        if step["op"] == "start_server":
            out.append(self._fact(ctx, "setup.server", {k: res.get(k) for k in ("endpoint", "bound_loopback", "listening", "ready_s", "resident_gib")}, "run_step"))
        return out

    async def _undo_step(self, args: dict, ctx: ToolContext) -> ProbeResult:
        n, why = self._step_number(args)
        if n is None:
            return ProbeResult(ok=False, error=why)
        done = [i for i, s in self.status.items() if s == "done"]
        if n not in done or n != max(done):
            return ProbeResult(ok=False, error="only the latest step that is done can be undone" + (f" (that is step {max(done)})" if done else " (nothing is done)"))
        ok, why = await self._approved(ctx, n, "undo")
        if not ok:
            return ProbeResult(ok=False, error=why)
        self._event(ctx, "setup_step", {"step": n, "action": "undo", "state": "approved"})
        rec = self.start_step(self.plan, n, "undo")
        await self._watch_step(ctx, rec, n, "undo")
        step = self.plan["steps"][n - 1]  # type: ignore[index]
        left = rec.result.get("left") if rec.status == "done" else None
        if rec.status != "done" or left:
            return ProbeResult(ok=False, facts=[self._fact(ctx, f"step.{n}", {"op": step["op"], "status": "undo_failed", "reason": _code_of(rec.reason), "left": left}, "undo_step")],
                               error=f"the undo of step {n} did not finish: " + (f"left behind: {left}" if left else _code_of(rec.reason)))
        self.status[n] = "undone"
        out = [self._fact(ctx, f"step.{n}", {"op": step["op"], "status": "undone", "left": []}, "undo_step"), self._ledger(ctx, "undo_step", n, "undo", "undone", left=[]), self._fact(ctx, "setup.verified", False, "undo_step")]
        if not any(s == "done" for s in self.status.values()):
            out.append(self._fact(ctx, "setup.undone_all", True, "undo_step"))  # nothing is left: every undo reported an empty `left`
        return ProbeResult(ok=True, facts=out)

    async def _finish(self, args: dict, ctx: ToolContext) -> ProbeResult:
        text = " ".join(str(args.get("summary", "")).split())[:300]
        if not text:
            return ProbeResult(ok=False, error="say what happened")
        return ProbeResult(ok=True, facts=[self._fact(ctx, "setup.finished", {"summary": text}, "finish_setup", status=FactStatus.hypothesis, by=CreatedBy.model, confidence=0.5)])


class UndoAdapter(SetupAdapter):
    """protocol-undo (docs/SETUP_API.md, "Undoing a finished setup"): the same guarded step machinery as the setup, started later and in a new run from the RECORD of a finished setup. Code reads the recorded plan and
    offers only the steps that are done, with the undo that plan itself wrote; the model chooses the order of its own calls from a menu of three, the owner approves every undo with a gesture, and the node removes
    exactly what the step wrote."""

    def __init__(self, *, node_id: str, node_name: str, load_setup, start_step, approve, owner_events) -> None:
        super().__init__(node_id=node_id, node_name=node_name, load_stored=lambda: None, load_pick=lambda: None, run_task=lambda *a, **k: None, start_step=start_step, approve=approve,
                         owner_events=owner_events, catalog={})
        self.load_setup = load_setup  # () -> the recorded setup {run, plan, status} or None
        self.offered: list[int] = []

    def tool_specs(self) -> list[ToolSpec]:
        k = [TargetKind.uninstall]
        return [A._spec("load_setup", "Read the recorded setup of this machine and make the undo plan.", kinds=k, produces=["plan.built", "setup.refused"]),
                A._spec("undo_step", "Undo the latest done step, owner approving.", kinds=k, requires=["plan.built"], repeatable=True, schema=A._schema(step=self._hint("undo"))),
                A._spec("finish_undo", "End the undo: everything is undone.", kinds=k, produces=["setup.finished"], schema=A._schema(summary="what happened, in a sentence"))]

    async def run(self, tool: str, args: dict[str, Any], ctx: ToolContext) -> ProbeResult:
        handler = {"load_setup": self._load_setup, "undo_step": self._undo_step, "finish_undo": self._finish}.get(tool)
        if handler is None:
            return ProbeResult(ok=False, error=f"unknown tool {tool}")
        try:
            return await handler(args, ctx)
        except Exception as exc:  # a tool never takes the run down
            return ProbeResult(ok=False, error=f"{type(exc).__name__}: {str(exc)[:200]}")

    async def _load_setup(self, args: dict, ctx: ToolContext) -> ProbeResult:
        rec = self.load_setup()
        todo = [] if not rec else [n for n, st in rec["status"].items() if st in ("done", "failed")]
        if not rec or not todo:
            return ProbeResult(ok=True, facts=[self._fact(ctx, "setup.refused", {"reason": "nothing_to_undo", "text": "this machine has no recorded setup with anything left to undo"}, "load_setup")])
        self.plan = rec["plan"]
        # a step that failed may have written part of what it was to write: its undo is the same rm of the same paths, so it is offered like a done one
        self.status = {int(n): ("done" if st in ("done", "failed") else st) for n, st in rec["status"].items()}
        steps = [x for x in self.plan["steps"] if self.status.get(x["n"]) == "done"]
        self.offered = [x["n"] for x in steps]  # the undo plan as it was when the run began: what is told to the owner does not shrink as the steps are undone
        self._event(ctx, "setup_plan", {"plan_hash": self.plan["plan_hash"], "steps": len(steps), "download_bytes": 0, "undo": True})
        summary = [{"n": x["n"], "op": x["op"], "title": x["title"]} for x in reversed(steps)]
        return ProbeResult(ok=True, facts=[self._fact(ctx, "plan.built", True, "load_setup"), self._fact(ctx, "plan.hash", self.plan["plan_hash"], "load_setup"), self._fact(ctx, "plan.steps", summary, "load_setup")])


class ConnectAdapter(SetupAdapter):
    """protocol-connect-chats: detect which chats are on this computer (read-only), put the owner's one closed question, have code build the plan from the recipes, then the same guarded steps as a setup, run on
    THIS computer by the coordinator's own code after the owner's approval of each step. The model chooses the order of its calls; it writes no command, path or entry."""

    def __init__(self, *, detect, build, preselected: str | None, start_step, approve, owner_events) -> None:
        super().__init__(node_id="this-computer", node_name="this computer", load_stored=lambda: None, load_pick=lambda: None, run_task=lambda *a, **k: None, start_step=start_step, approve=approve,
                         owner_events=owner_events, catalog={})
        self.detect, self.build, self.preselected = detect, build, preselected  # detect() -> rows; build(host) -> plan or raises ConnectRefused
        self.host: str | None = None
        self.rows: list[dict] = []

    @property
    def record_id(self) -> str | None:
        return f"connect-{self.host}" if self.host else None

    def tool_specs(self) -> list[ToolSpec]:
        k = [TargetKind.chat_connect]
        named = self.preselected is not None  # the owner named the chat in the request: that is their answer, so there is no question to put (and no tool for it)
        return [A._spec("detect_hosts", "Look for chats that can be connected (read-only).", kinds=k, produces=["connect.detected"] + (["connect.choice"] if named else [])),
                *([] if named else [A._spec("ask_hosts", "Ask the owner which found chat to connect.", kinds=k, requires=["connect.detected"], produces=["connect.choice", "setup.refused"], asks_owner=True)]),
                A._spec("build_connect_plan", "Make the connection plan from the recipes.", kinds=k, requires=["connect.choice"], produces=["plan.built", "setup.refused"]),
                A._spec("run_step", "Run one plan step once the owner approves.", kinds=k, requires=["plan.built"], repeatable=True, schema=A._schema(step=self._hint("run"))),
                A._spec("undo_step", "Undo the latest done step, owner approving.", kinds=k, requires=["plan.built"], repeatable=True, schema=A._schema(step=self._hint("undo"))),
                A._spec("finish_connect", "End the connection: verified, or all undone.", kinds=k, produces=["setup.finished"], schema=A._schema(summary="what happened, in a sentence"))]

    async def run(self, tool: str, args: dict[str, Any], ctx: ToolContext) -> ProbeResult:
        handler = {"detect_hosts": self._detect, "ask_hosts": self._ask_hosts, "build_connect_plan": self._build_connect, "run_step": self._run_step, "undo_step": self._undo_step, "finish_connect": self._finish}.get(tool)
        if handler is None:
            return ProbeResult(ok=False, error=f"unknown tool {tool}")
        try:
            return await handler(args, ctx)
        except Exception as exc:  # a tool never takes the run down
            return ProbeResult(ok=False, error=f"{type(exc).__name__}: {str(exc)[:200]}")

    def _after_step(self, step: dict, res: dict, ctx: ToolContext) -> list[Fact]:
        return [self._fact(ctx, "setup.verified", bool(res.get("ok")), "run_step")] if step["kind"] == "verify" else []

    async def _detect(self, args: dict, ctx: ToolContext) -> ProbeResult:
        self.rows = await asyncio.to_thread(self.detect)
        value = [{k: r.get(k) for k in ("host", "label", "state", "detail", "running", "code")} for r in self.rows]
        facts = [self._fact(ctx, "connect.detected", value, "detect_hosts")]
        if self.preselected:  # the owner's request is their answer (recorded as an OWNER event when the run started): the choice is read from it, and an unknown chat is refused here
            if self.preselected not in {r["host"] for r in self.rows}:
                return ProbeResult(ok=True, facts=[*facts, self._fact(ctx, "setup.refused", {"reason": "unknown_host", "text": f"there is no recipe for a chat called {self.preselected!r}"}, "detect_hosts")])
            self.host = self.preselected
            facts.append(self._fact(ctx, "connect.choice", {"host": self.host, "by": "the owner's request"}, "detect_hosts", by=CreatedBy.human))
        return ProbeResult(ok=True, facts=facts)

    async def _ask_hosts(self, args: dict, ctx: ToolContext) -> ProbeResult:
        offered = [r for r in self.rows if r["state"] in ("found", "installable")]
        if not offered:
            return ProbeResult(ok=True, facts=[self._fact(ctx, "setup.refused", {"reason": "no_host_found", "text": "no chat that can be connected was found on this computer"}, "ask_hosts")])
        if self.pending is not None:
            return ProbeResult(ok=False, error="a question is already waiting for the owner")
        options = [{"id": r["host"], "label": r["label"] + (" (will be installed)" if r["state"] == "installable" else ""), "detail": r["detail"]} for r in offered]
        answer = await self._ask(ctx, "which_chats", "Which chats do you want to use Multi Auto Setup in?", options)
        if answer is None:
            return ProbeResult(ok=False, error="the owner did not answer in time")
        by, channel = self.last_answered_by, getattr(self, "last_answered_channel", None)
        self._owner(ctx.store, "owner_answer", {"question": "which_chats", "option": answer, **({"by": by} if by != "owner" else {}), **({"channel": channel} if channel else {})})
        self.host = answer
        return ProbeResult(ok=True, facts=[self._fact(ctx, "connect.choice", {"host": answer, "by": "the owner" if by == "owner" else by}, "ask_hosts", by=CreatedBy.human)])

    async def _build_connect(self, args: dict, ctx: ToolContext) -> ProbeResult:
        from envrec_coordinator.connect import ConnectRefused
        if not self.host:
            return ProbeResult(ok=False, error="ask the owner which chat first")
        try:
            plan = await asyncio.to_thread(self.build, self.host)
        except ConnectRefused as exc:  # the protocol's exit `nothing_to_connect` reads this fact; code said no, with a typed reason
            return ProbeResult(ok=True, facts=[self._fact(ctx, "setup.refused", {"reason": exc.code, "text": exc.text}, "build_connect_plan")])
        self.plan = plan
        self.status = {s["n"]: "pending" for s in plan["steps"]}
        self._event(ctx, "setup_plan", {"plan_hash": plan["plan_hash"], "steps": len(plan["steps"]), "download_bytes": sum(x["bytes"] for x in plan["steps"] if x.get("size_kind") != "move"),
                                   "moved_bytes": sum(x["bytes"] for x in plan["steps"] if x.get("size_kind") == "move")})
        summary = [{"n": s["n"], "op": s["op"], "title": s["title"]} for s in plan["steps"]]
        return ProbeResult(ok=True, facts=[self._fact(ctx, "plan.built", True, "build_connect_plan"), self._fact(ctx, "plan.hash", plan["plan_hash"], "build_connect_plan"), self._fact(ctx, "plan.steps", summary, "build_connect_plan")])

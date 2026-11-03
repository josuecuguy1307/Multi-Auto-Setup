"""Install (or undo the install of) a command-line tool on a paired computer, as a services tool (mixed into `Services`). CODE builds the plan from the catalog entry and the machine's analysis; the owner
approves the WHOLE plan in ONE chat question bound to its hash; only then does code send the steps, in order, each one a `setup_step` the node accepts only with that approval in its token. A model never
approves and never writes a step. What an install leaves is recorded per computer, so the router sees the tool as installed there and an undo has something to undo."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from envrec_coordinator import options as O
from envrec_coordinator import tool_plan as TP
from envrec_net import envrun as E
from envrec_net import setup as P

STEP_WAIT_S = 1200.0
TI_NO_ENV = "The catalog has no environment named {environment}."
TI_NO_ANALYSIS = "There is no stored analysis of {machine}: analyse it first, so the harness knows its system and architecture."
TI_NO_MACHINE = "Say which computer: {names}."
TI_REFUSED = "{environment} cannot be installed on {machine}: {state}: {why}"
TI_NOT_INSTALLED = "{environment} is not installed on {machine} by the harness, so there is nothing to undo."
TI_ASK = "Install {title} on {machine}? Plan {hash}: {n} step(s), {mb} MB to download, into its own folder under the node's folder (no administrator). {answer}"
TI_UNDO_ASK = "Remove {title} from {machine}? Plan {hash}. {answer}"
TI_DECLINED = "The owner did not approve it, so nothing was written."
TI_DONE = "{title} is installed on {machine}: it starts (the check printed: {line}). Undo it with mas_install_tool (action undo)."
TI_UNDONE = "{title} was removed from {machine}."
TI_FAILED = "Step {n} ({title}) did not succeed: {why}. Nothing else was run. Steps that did run are still in place; undo removes them."
TI_OFFLINE = "{machine} is not connected right now."


class ToolInstallTools:
    def _tool_node(self, machine: str | None) -> tuple[str | None, str]:
        regs = self.coord.registry.all()
        if machine:
            try:
                return self.coord.harness._resolve_node(machine), ""
            except Exception:  # noqa: BLE001 - an unknown or ambiguous name is answered below
                return None, TI_NO_MACHINE.format(names=", ".join(r.name for r in regs) or "none paired")
        if len(regs) == 1:
            return regs[0].node_id, ""
        return None, TI_NO_MACHINE.format(names=", ".join(r.name for r in regs) or "none paired")

    def install_tool(self, ch, *, environment: str, machine: str | None = None, action: str = "install") -> dict:
        entry = self.catalog.get(environment) if hasattr(self, "catalog") else None
        if entry is None:
            return self._out(False, TI_NO_ENV.format(environment=environment))
        nid, why = self._tool_node(machine)
        if nid is None:
            return self._out(False, why)
        name = self._name(nid)
        if action == "undo":
            return self._tool_undo(ch, entry, nid, name)
        analysis = self.coord.analysis_of(nid)
        if analysis is None:
            return self._out(False, TI_NO_ANALYSIS.format(machine=name))
        n = O._norm({k: row.get("value") for k, row in (analysis.get("facts") or {}).items()})
        permissive = set(E.load_policy().get("permissive_licenses", []))
        try:
            plan = TP.build_tool_plan(entry, n, TP.load_tool_recipes(), permissive, machine_id=nid, installed=next((m.installed for m in self.facts.machines() if m.machine_id == nid), set()))
        except TP.PlanRefused as exc:
            self.coord.log.add("tool_install_refused", {"environment": environment, "node": nid, "reason": exc.reason, "tag": "GUARD"})
            return self._out(False, TI_REFUSED.format(environment=environment, machine=name, state=exc.reason, why=exc.text))
        return self._tool_ask(ch, entry, nid, name, plan, "run")

    def _tool_undo(self, ch, entry: dict, nid: str, name: str) -> dict:
        rec = self.coord.runs.stored_tools(nid).get(entry["id"])
        if rec is None:
            return self._out(False, TI_NOT_INSTALLED.format(environment=entry["id"], machine=name))
        return self._tool_ask(ch, entry, nid, name, P.parse_plan(rec["plan"]), "undo")

    def _tool_ask(self, ch, entry: dict, nid: str, name: str, plan: P.SetupPlan, action: str) -> dict:
        steps = plan.steps if action == "run" else list(reversed(plan.steps))
        shown = lambda t: str(t).replace("<mas>", "[the node's folder]")  # noqa: E731 - a question a chat shows holds no angle brackets; the plan itself (and its hash) keeps the real path
        lines = [f"{s.n}. {s.title}: {shown(s.exact_command if action == 'run' else s.undo)} (writes {shown(', '.join(s.writes)) or 'nothing'}{f', {round(s.bytes / 1e6, 1)} MB' if s.bytes else ''})" for s in steps]
        mb = round(sum(s.bytes for s in plan.steps) / 1e6, 1)
        kind = "tool_install" if action == "run" else "tool_uninstall"
        conf = self.coord.harness.request_confirmation(None, summary=(f"Install {entry['title']} on {name}" if action == "run" else f"Remove {entry['title']} from {name}"),
                                                       plan_hash=plan.plan_hash, command="\n".join(lines), undo=shown("; ".join(f"{s.n}: {s.undo}" for s in plan.steps)), sensitivity="internal", kind=kind)
        jid = f"tool-{plan.plan_hash[7:19]}-{action}"
        self._job(jid, kind, "awaiting_approval", intent_id=conf.intent.id, sensitivity="internal", data={"environment": entry["id"], "machine": name, "plan_hash": plan.plan_hash, "steps": len(plan.steps), "mb": mb})
        conf.future.add_done_callback(lambda f, j=jid, p=plan, e=entry, a=action, x=nid: self._tool_decided(j, p, e, x, a, f.result()))
        ask = (TI_ASK if action == "run" else TI_UNDO_ASK).format(title=entry["title"], machine=name, hash=plan.plan_hash[7:19], n=len(plan.steps), mb=mb, answer=self.coord.harness.answer_how(conf.intent.id))
        return self._out(True, ask + "\n" + "\n".join(lines), job_id=jid, intent_id=conf.intent.id, plan_hash=plan.plan_hash)

    def _tool_decided(self, jid: str, plan: P.SetupPlan, entry: dict, nid: str, action: str, ok: bool) -> None:
        job = self.jobs[jid]
        if not ok:
            job.state, job.text = "declined", TI_DECLINED
            return
        job.state = "running"
        asyncio.ensure_future(self._tool_steps(job, plan, entry, nid, action))

    async def _tool_steps(self, job, plan: P.SetupPlan, entry: dict, nid: str, action: str) -> None:
        name = self._name(nid)
        order = [s.n for s in plan.steps] if action == "run" else [s.n for s in reversed(plan.steps)]
        last: dict[str, Any] = {}
        for n in order:
            step = plan.step(n)
            self.coord.log.add("setup_step_approved", {"run": job.id, "node": nid, "plan_hash": plan.plan_hash, "step": n, "action": action}, owner=True)  # the owner's one answer covers the steps of this exact plan
            rec = self.coord.tasks.start_setup_step(nid, plan.model_dump(), n, action)
            try:
                await asyncio.wait_for(rec.done.wait(), STEP_WAIT_S)
            except TimeoutError:
                await self.coord.tasks.cancel(rec.id)
            if rec.status != "done":
                job.state = "failed"
                job.text = TI_OFFLINE.format(machine=name) if rec.reason == "node_offline" else TI_FAILED.format(n=n, title=step.title, why=rec.reason or rec.status)
                self.coord.log.add("tool_install_failed", {"job": job.id, "step": n, "action": action, "reason": rec.reason or rec.status, "tag": "GUARD"})
                return
            last = rec.result or {}
        if action == "run":
            self.coord.runs.save_tool(nid, entry["id"], {"plan": plan.model_dump(), "installed_at": time.time(), "check": last.get("output", "")})
            job.state, job.text = "done", TI_DONE.format(title=entry["title"], machine=name, line=str(last.get("output", ""))[:100])
        else:
            self.coord.runs.drop_tool(nid, entry["id"])
            job.state, job.text = "done", TI_UNDONE.format(title=entry["title"], machine=name)

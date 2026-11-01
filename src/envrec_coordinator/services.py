"""The coordinator's services: the router, the admission desk, the access flows and the tools the owner's chats call, wired to the coordinator's registry, task desk and log.

Nothing here decides a route, an escalation or an approval. The router (code, a written rule) decides routes; the owner's human gesture approves a run, an exam or an access change; a tool call only asks
for one. A chat's channel (never a tool argument) says whether the task comes from a cloud chat. Every tool returns `text` that stands alone (no card is needed to read it) and the data behind it."""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from envrec_coordinator import services_strings as S
from envrec_coordinator.admission import router_bridge as bridge
from envrec_coordinator.admission.decision import AdmissionDesk
from envrec_coordinator.admission.decision import Refusal as AdmissionRefusal
from envrec_coordinator.admission.exam import ExamAborted, Reply, RunnerAborted
from envrec_coordinator.admission.facts import AdmissionFacts, Combo
from envrec_coordinator.admission.suite import SuiteRefused, load_suite
from envrec_coordinator.router import EntryChat, MemoryOutputs, Policy, Refusal, Route, RouteDecision, Router, Task, filter_outbound
from envrec_coordinator.router import grading
from envrec_coordinator.router.catalog import build_candidates
from envrec_coordinator.live_facts import CoordinatorFacts
from envrec_net import envrun as E
from envrec_net import task as T

MAX_TEXT = 100_000
MAX_CHECK = 200_000
CHECK_KINDS = grading.TEXT_KINDS + grading.EXECUTING_KINDS
ATTEMPT_WAIT_S = 1500.0


class LoopLog:
    """The coordinator's event log as a worker thread sees it. The log's database belongs to the event loop's thread, and an admission exam runs in a worker thread (it waits on the models), so every
    call from there is handed to the loop and its answer waited for. From the loop's own thread the call goes straight through."""

    def __init__(self, log) -> None:
        self._log, self.loop = log, None

    def _call(self, fn, *a, **k):
        loop = self.loop
        try:
            if loop is None or asyncio.get_running_loop() is loop:
                return fn(*a, **k)
        except RuntimeError:  # no loop in this thread: a worker
            pass
        fut: concurrent.futures.Future = concurrent.futures.Future()

        def run() -> None:
            try:
                fut.set_result(fn(*a, **k))
            except BaseException as exc:  # noqa: BLE001 - handed back to the worker
                fut.set_exception(exc)
        loop.call_soon_threadsafe(run)
        return fut.result(60)

    def add(self, *a, **k):
        return self._call(self._log.add, *a, **k)

    def events(self, *a, **k):
        return self._call(self._log.events, *a, **k)

    @property
    def store(self):
        outer = self

        class Store:
            def put_raw(self, *a, **k):
                return outer._call(outer._log.store.put_raw, *a, **k)
        return Store()


@dataclass
class Job:
    id: str
    kind: str
    state: str
    text: str = ""
    intent_id: str | None = None
    sensitivity: str = "internal"
    data: dict = field(default_factory=dict)


def _digest(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


class Services:
    def __init__(self, coord, *, envs: dict[str, dict] | None = None, policy: Policy | None = None, sandbox_ready=None, sandbox_runner=None) -> None:
        self.coord = coord
        self.envs = envs if envs is not None else E.load_environments()
        self.policy = policy or Policy.defaults()
        self._loop_log = LoopLog(coord.log)
        self.store = AdmissionFacts(self._loop_log, coord.clock)
        self.facts = CoordinatorFacts(coord, self.store, self.envs)
        self.outputs = MemoryOutputs()
        self.payloads: dict[str, str] = {}
        self.jobs: dict[str, Job] = {}
        self.tasks: dict[str, dict] = {}
        self.router = Router(envs=self.envs, facts=self.facts, policy=self.policy, log=self._log, sender=coord.tasks, payloads=self.payloads.get, outputs=self.outputs,
                             sandbox_ready=sandbox_ready or bridge.sandbox_ready, sandbox_runner=sandbox_runner or bridge.sandbox_runner)
        self.desk = AdmissionDesk(self.store, sandbox_ready=sandbox_ready or bridge.sandbox_ready)
        self._creds_at: dict[str, float] = {}
        self.boards: dict[str, dict] = {}
        self.director_factory = None  # (board id) -> a callable reading the board's view and answering one action; None means the coordinator's own model
        limit = getattr(coord, "board_escalation_limit", self.policy.max_escalation)  # the coordinator settings key board_escalation_limit
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError(f"board_escalation_limit must be an integer, got {type(limit).__name__}")
        self.board_preapproved = max(0, min(limit, self.policy.max_escalation))  # escalation steps a board may take without asking the owner: 0 to the router's maximum, outside that it is clamped. Steps, not money

    # ---- plumbing ------------------------------------------------------------------------------------------------------------------------------

    def _log(self, type_: str, payload: dict, actor: str) -> None:
        self._loop_log.add(type_, {**payload, "tag": actor})  # a grade runs in a worker thread: its event goes through the loop's thread, which owns the log

    def _name(self, machine_id: str) -> str:
        rec = self.coord.registry.get(machine_id)
        return rec.name if rec is not None else machine_id

    def _key_text(self, key: str) -> str:
        env, model, access, machine = key.split("|")
        return f"{env} ({model}) via {access} on {self._name(machine)}"

    @staticmethod
    def _out(ok: bool, text: str, **data: Any) -> dict:
        return {"ok": ok, "text": text, **data}

    def _job(self, job_id: str, kind: str, state: str, **kw: Any) -> Job:
        job = self.jobs[job_id] = Job(job_id, kind, state, **kw)
        return job

    def _confirmation_mechanism(self, plan_hash: str) -> str | None:
        return next((e.payload.get("mechanism") for e in reversed(self.coord.log.events("owner_confirmation")) if e.payload.get("plan_hash") == plan_hash and e.payload.get("approved")), None)

    async def refresh_credentials(self, node_id: str | None = None) -> None:
        """Ask each node which credential NAMES it holds (its store or its environment), so a route can say "access missing: NAME". Names only, never a value."""
        names = sorted({n for e in self.envs.values() for a in e.get("accesses", []) for n in [*a.get("env_names", []), *a.get("env_names_any", [])]})
        for rec in self.coord.registry.all():
            if node_id and rec.node_id != node_id:
                continue
            if rec.node_id not in self.coord._sessions:
                continue
            r = self.coord.tasks.start(rec.node_id, "credential_presence", {"names": names}, by="GUARD")
            try:
                await asyncio.wait_for(r.done.wait(), 20)
            except TimeoutError:
                continue
            if r.status == "done":
                self.facts.credential_names[rec.node_id] = set(r.result.get("names", []))
                self._creds_at[rec.node_id] = time.monotonic()

    def _stale_credentials(self) -> None:
        if any(time.monotonic() - self._creds_at.get(r.node_id, -1e9) > 60 for r in self.coord.registry.all()):
            asyncio.ensure_future(self.refresh_credentials())

    # ---- the reasoning, as data and as text ------------------------------------------------------------------------------------------------------

    def _explain_data(self, res: RouteDecision | Refusal, *, task_type: str, sensitivity: str | None, chat: EntryChat) -> dict:
        if isinstance(res, Refusal):
            d = res.details
            return {"task_type": task_type, "sensitivity": sensitivity, "entry_chat": chat.kind, "candidates": list(d.get("candidates", [])), "left_out": dict(d.get("environments", {})),
                    "dropped": dict(d.get("dropped", {})), "filters": [], "chosen": None, "refusal": {"code": res.code, "why": d.get("why"), "reason": res.reason}}
        trace = [s.model_dump() for s in res.rule_trace]
        costs = next((s["values"] for s in trace if s["step"] == "cost_class"), {})
        return {"task_type": task_type, "sensitivity": sensitivity, "effective_sensitivity": res.effective_sensitivity, "entry_chat": chat.kind, "candidates": list(res.rule_trace[0].candidates_in),
                "left_out": dict(res.left_out), "filters": trace, "chosen": res.route.key, "cost_class": costs.get(res.route.key), "evidence": res.evidence_label, "provisional": res.provisional,
                "escalation_step": res.escalation_step, "refusal": None}

    def _explain_text(self, data: dict) -> str:
        lines = [S.EXPLAIN_HEAD.format(task_type=data["task_type"], sensitivity=data["sensitivity"], chat=data["entry_chat"])]
        cands = data["candidates"]
        lines.append(S.EXPLAIN_CANDIDATES.format(n=len(cands)) if cands else S.EXPLAIN_NONE)
        lines += [f"  - {self._key_text(k)}" for k in cands]
        if data["left_out"]:
            lines.append(S.EXPLAIN_LEFT.format(n=len(data["left_out"])))
            lines += [f"  - {tag.split('@')[0]} on {self._name(tag.split('@')[1])}: {why}" for tag, why in data["left_out"].items()]
        for st in data["filters"]:
            cut = {k: v for k, v in st["values"].items() if k in st["candidates_in"] and k not in st["candidates_out"]} if st["step"] in ("sensitivity", "exclusion", "admission_filter") else {}
            detail = "; " + ", ".join(f"{self._key_text(k)}: {v}" for k, v in cut.items()) if cut else (f" ({st['note']})" if st["step"] == "cost_class" and st["values"] else "")
            if st["step"] == "cost_class":
                detail = "; " + ", ".join(f"{self._key_text(k)} is {v}" for k, v in st["values"].items())
            lines.append(S.EXPLAIN_STEP.format(step=st["step"], kept=len(st["candidates_out"]), total=len(st["candidates_in"]), detail=detail))
        if data["refusal"]:
            lines.append(S.EXPLAIN_REFUSED.format(code=data["refusal"]["code"], reason=data["refusal"]["reason"]))
            if data["dropped"]:
                lines += [f"  - {self._key_text(k)}: {v}" for k, v in data["dropped"].items()]
        else:
            lines.append(S.EXPLAIN_CHOSEN.format(route=self._key_text(data["chosen"]), cost_class=data["cost_class"], evidence=data["evidence"], provisional=S.PROVISIONAL if data["provisional"] else "",
                                                 step=data["escalation_step"]))
        return "\n".join(lines)

    def _chat(self, ch) -> EntryChat:
        return EntryChat(kind="cloud" if ch.cloud else "local", name=ch.name)  # the channel's own property: never an argument

    # ---- tools -----------------------------------------------------------------------------------------------------------------------------------

    def explain_route(self, ch, *, task_id: str | None = None, task_type: str | None = None, sensitivity: str | None = None) -> dict:
        """The route a task would take right now, with the reasoning: candidates, who was left out and why, what each filter cut, what the written rule chose. Records nothing."""
        self._stale_credentials()
        chat = self._chat(ch)
        if task_id:
            st = self.tasks.get(task_id)
            if st is None:
                return self._out(False, S.STATUS_UNKNOWN.format(job_id=task_id))
            task = st["task"]
        else:
            if not task_type or not isinstance(task_type, str):
                return self._out(False, "Say which task type (and sensitivity) to explain, or give a task_id.")
            task = Task(task_id="explain-" + secrets.token_hex(3), task_type=task_type[:40], sensitivity=sensitivity, entry_chat=chat, created_by="OWNER", payload_ref="explain")
        res = self.router.explain(task)
        data = self._explain_data(res, task_type=task.task_type, sensitivity=task.sensitivity, chat=task.entry_chat)
        return self._out(True, self._explain_text(data), **data)

    def create_task(self, ch, *, text: str, task_type: str, sensitivity: str, check: dict | None = None) -> dict:
        """Create a task. Nothing runs: the owner approves the exact route in his own system dialog first."""
        self._stale_credentials()
        task_id = "t-" + secrets.token_hex(4)
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
            return self._out(False, S.TASK_REFUSED.format(task_id=task_id, reason=f"the text must be 1 to {MAX_TEXT} characters"))
        spec = None
        if check is not None:
            kind = check.get("kind") if isinstance(check, dict) else None
            if kind not in CHECK_KINDS or ("files" if kind == "project_tests" else "value") not in check:
                return self._out(False, S.TASK_REFUSED.format(task_id=task_id, reason=f"a check is one of {', '.join(CHECK_KINDS)}, graded by code"))
            if len(json.dumps(check, default=str)) > MAX_CHECK:
                return self._out(False, S.TASK_REFUSED.format(task_id=task_id, reason="the check is too large"))
            spec = dict(check)
        chat = self._chat(ch)
        ref = f"payload:{task_id}"
        self.payloads[ref] = text
        task = Task(task_id=task_id, task_type=str(task_type)[:40], sensitivity=sensitivity if isinstance(sensitivity, str) else None, entry_chat=chat, created_by="OWNER", payload_ref=ref)
        res = self.router.explain(task)
        data = self._explain_data(res, task_type=task.task_type, sensitivity=task.sensitivity, chat=chat)
        if isinstance(res, Refusal):
            self.payloads.pop(ref, None)
            self.coord.log.add("task_refused", {"task_id": task_id, "refusal": res.code, "why": res.details.get("why"), "channel": ch.name, "tag": "GUARD"})
            self._job(task_id, "task", "refused", text=res.reason, sensitivity=task.sensitivity or "internal", data=data)
            return self._out(False, S.TASK_REFUSED.format(task_id=task_id, reason=res.reason) + "\n" + self._explain_text(data), task_id=task_id, **data)
        plan_hash = "sha256:" + _digest(task_id, res.route.key, res.effective_sensitivity, spec and spec.get("kind"), spec and _digest(spec.get("value"), spec.get("files")))
        pending = self.coord.harness.request_confirmation(
            None, summary=f"Run task {task_id} ({task.task_type}, {res.effective_sensitivity}) on {self._key_text(res.route.key)}?", plan_hash=plan_hash,
            command=f"run it there (evidence: {res.evidence_label}{', provisional' if res.provisional else ''})", undo="nothing to undo: it works in a folder that is deleted when it ends", sensitivity=res.effective_sensitivity)
        self.tasks[task_id] = {"task": task, "check": spec, "plan_hash": plan_hash, "approved_route": res.route.key, "attempts": [], "intent_id": pending.intent.id}
        if spec:
            self.router.set_grader(task_id, spec)
        self._job(task_id, "task", "awaiting_approval", intent_id=pending.intent.id, sensitivity=res.effective_sensitivity, data=data)
        pending.future.add_done_callback(lambda f, t=task_id: self._approved(t, f.result()))
        route_line = S.ROUTE_LINE.format(route=self._key_text(res.route.key), cost_class=data["cost_class"], evidence=res.evidence_label, provisional=S.PROVISIONAL if res.provisional else "")
        check_line = S.CHECK_NONE
        if spec:
            check_line = S.CHECK_SET.format(kind=spec["kind"])
            if spec["kind"] in grading.EXECUTING_KINDS:  # the answer comes back as text; any code in it runs only in this computer's sandbox, never on the machine that answered
                check_line += " " + (S.CHECK_SANDBOX if self.router.sandbox_ready() else S.CHECK_NO_SANDBOX).format(machine=self._name(res.route.machine_id))
        return self._out(True, S.TASK_CREATED.format(task_id=task_id, task_type=task.task_type, sensitivity=res.effective_sensitivity, route_line=route_line, intent_id=pending.intent.id, check_line=check_line)
                         + "\n" + self._explain_text(data), task_id=task_id, intent_id=pending.intent.id, **data)

    def _approved(self, task_id: str, ok: bool) -> None:
        job = self.jobs[task_id]
        if not ok:
            job.state, job.text = "declined", "The owner did not approve it, so nothing ran."
            return
        self.coord.own_task(task_id, by="owner")  # an OWNER event: the owner's own gesture just approved this exact route
        job.state = "running"
        asyncio.ensure_future(self._run_task(task_id))

    async def _run_task(self, task_id: str) -> None:
        st, job = self.tasks[task_id], self.jobs[task_id]
        self._loop_log.loop = asyncio.get_running_loop()
        task: Task = st["task"]
        try:
            decision = self.router.route(task)
            if isinstance(decision, RouteDecision) and decision.route.key != st["approved_route"]:
                job.state, job.text = "route_changed", f"The facts changed after you approved: the rule now picks {self._key_text(decision.route.key)}, not {self._key_text(st['approved_route'])}. Create the task again to approve the new route."
                return
            while True:
                if isinstance(decision, Refusal):
                    job.state, job.text = ("exhausted" if decision.code == "escalation_exhausted" else "refused"), decision.reason
                    job.data["notice"] = decision.details.get("owner_notice", {}).get("text_fallback")
                    return
                handle = self.router.dispatch(decision)
                if isinstance(handle, Refusal):
                    job.state, job.text = "refused", handle.reason
                    return
                rec = self.coord.tasks.tasks[handle.node_task_id]
                try:
                    await asyncio.wait_for(rec.done.wait(), ATTEMPT_WAIT_S)
                except TimeoutError:
                    pass
                attempt = {"step": decision.escalation_step, "route": decision.route.key, "state": rec.status, "reason": rec.reason or None, "graded": None}
                st["attempts"].append(attempt)
                if rec.status == "done":
                    out = self.router.result_of(handle)
                    attempt["output_ref"] = out["output_ref"]
                    job.data["output"] = self.outputs.get(out["output_ref"]).text
                    if st["check"] is None:
                        job.state, job.text = "done_unchecked", "The environment finished. No code check was given, so the result is shown but not graded."
                        return
                    verdict = await asyncio.to_thread(self.router.grade, task_id, out["output_ref"])
                    if isinstance(verdict, Refusal):
                        job.state, job.text = "unchecked", f"The environment finished but the check could not run: {verdict.reason}"
                        attempt["graded"] = verdict.code
                        return
                    attempt["graded"] = verdict.passed
                    grader = verdict.model_dump()
                else:
                    grader = {"grader": "result_verification", "passed": False, "evidence_ref": _digest(task_id, rec.id, rec.status, rec.reason)}
                nxt = self.router.report_result(task_id, decision.escalation_step, grader)
                if nxt is None:
                    job.state, job.text = "done", f"Passed its code check on step {decision.escalation_step} ({self._key_text(decision.route.key)})."
                    return
                decision = nxt
        except Exception as exc:  # noqa: BLE001 - a job that cannot go on says so; it never takes the coordinator down
            job.state, job.text = "failed", f"The run stopped: {type(exc).__name__}."

    def job_status(self, ch, job_id: str) -> dict:
        job = self.jobs.get(job_id)
        if job is None:
            return self._out(False, S.STATUS_UNKNOWN.format(job_id=job_id))
        lines = [S.JOB_STATE.format(job_id=job.id, kind=job.kind, state=job.state)]
        if job.text:
            lines.append(job.text)
        if job.intent_id and job.state in ("awaiting_approval", "awaiting_owner"):
            lines.append(f"Waiting for you: call mas_request_approval with intent_id {job.intent_id}.")
        st = self.tasks.get(job.id)
        if st:
            for a in st["attempts"]:
                lines.append(f"Step {a['step']}: {self._key_text(a['route'])}: {a['state']}" + (f" ({a['reason']})" if a["reason"] else "") + (f", code check {'passed' if a['graded'] else 'failed'}" if isinstance(a["graded"], bool) else ""))
        data = {k: v for k, v in job.data.items() if k != "output"}
        if "output" in job.data:
            shown = filter_outbound({"kind": "text", "actor": "GUARD", "sensitivity": job.sensitivity, "payload": {"output": job.data["output"], "task_id": job.id},
                                     "text_fallback": job.data["output"]}, {"kind": "cloud" if ch.cloud else "local", "name": ch.name}, outbound_policy=self.policy.outbound)
            if shown.get("redacted"):
                lines.append(S.OUTPUT_WITHHELD)
            else:
                lines.append("Result:\n" + job.data["output"][:6000])
                data["output"] = job.data["output"]
        return self._out(True, "\n".join(lines), job_id=job.id, state=job.state, attempts=(st or {}).get("attempts", []), **data)

    # ---- admission: measure a combination on a suite, after the owner's approval ----------------------------------------------------------------

    def run_admission(self, ch, *, environment: str, task_type: str, machine: str | None = None, access: str | None = None) -> dict:
        """Propose the exam of one combination on its task type's suite. It uses the owner's machine time (and a plan's quota), so it starts only after his own approval."""
        self._stale_credentials()
        try:
            suite = load_suite(task_type)
        except (SuiteRefused, ValueError):
            return self._out(False, S.ADMISSION_NO_SUITE.format(task_type=task_type))
        probe = Task(task_id="adm-probe", task_type=task_type, sensitivity="public", entry_chat=EntryChat(kind="local", name="admission"), created_by="OWNER", payload_ref="admission")
        detail: dict = {}
        cands, left = build_candidates(probe, self.envs, self.facts, self.policy, detail)
        node = None
        if machine:
            try:
                node = self.coord.harness._resolve_node(machine)
            except Exception:  # noqa: BLE001 - an unknown or ambiguous machine name is answered below
                return self._out(False, S.ADMISSION_NO_COMBO.format(environment=environment, task_type=task_type, why=f"{machine} is not one of your paired machines"))
        pick = [c for c in cands if c.route.environment_id == environment and (node is None or c.route.machine_id == node) and (access is None or c.route.access_id == access)]
        if len(pick) != 1:
            why = ("; ".join(f"{t}: {r}" for t, r in left.items() if t.startswith(environment + "@")) or "it is not a candidate on any machine") if not pick else "more than one combination fits: say the machine or the access"
            return self._out(False, S.ADMISSION_NO_COMBO.format(environment=environment, task_type=task_type, why=why))
        r = pick[0].route
        sha, version = self.facts._current(r.key)
        combo = Combo(task_type, r.machine_id, r.environment_id, r.model_id, r.access_id, sha, version)
        pending = self.desk.propose(combo, suite, machine_name=self._name(r.machine_id))
        aid = pending["id"]
        plan_hash = "sha256:" + hashlib.sha256(aid.encode()).hexdigest()
        conf = self.coord.harness.request_confirmation(None, summary=pending["text"], plan_hash=plan_hash, command=f"run the {suite.suite_id} exam on {self._key_text(r.key)}",
                                                       undo="nothing to undo: it only records a measurement", sensitivity="public")
        self._job(aid, "admission", "awaiting_owner", intent_id=conf.intent.id, sensitivity="public", data={"combo": combo.key, "suite": suite.suite_id, "suite_hash": suite.hash})
        conf.future.add_done_callback(lambda f, a=aid, c=combo, s=suite, ph=plan_hash, chn=ch.name: self._admission_decided(a, c, s, ph, chn, f.result()))
        return self._out(True, S.ADMISSION_ASK.format(text=pending["text"], intent_id=conf.intent.id), job_id=aid, intent_id=conf.intent.id, combo=combo.key)

    def _admission_decided(self, aid: str, combo: Combo, suite, plan_hash: str, channel: str, ok: bool) -> None:
        job = self.jobs[aid]
        mechanism = self._confirmation_mechanism(plan_hash)
        self.desk.answer(aid, "approve" if ok else "reject", by="owner", gesture=mechanism, channel=channel)  # the gesture is the one the ticket recorded; a bare answer would be refused
        if not ok:
            job.state, job.text = "declined", S.ACCESS_DECLINED
            return
        job.state = "running"
        asyncio.ensure_future(self._run_exam(aid, combo, suite))

    async def _run_exam(self, aid: str, combo: Combo, suite) -> None:
        job, loop = self.jobs[aid], asyncio.get_running_loop()
        self._loop_log.loop = loop
        services = self

        class Runner:
            def ask(self, prompt: str, *, task_id: str, timeout_s: float) -> Reply:
                fut = asyncio.run_coroutine_threadsafe(services._ask(aid, combo, prompt, task_id, timeout_s), loop)
                return fut.result(timeout_s + 120)

        try:
            out = await asyncio.to_thread(self.desk.start, combo, suite, Runner())
            label = self.store.view(combo, suite, out.threshold).label
            job.state, job.text = "done", S.ADMISSION_DONE.format(environment=combo.environment_id, task_type=combo.task_type, label=label)
            job.data["passed"], job.data["of"], job.data["admitted"] = out.passed, out.of, out.admitted
        except ExamAborted as exc:
            job.state, job.text = "aborted", S.ADMISSION_ABORTED.format(environment=combo.environment_id, task_type=combo.task_type, reason=exc.reason)
        except AdmissionRefusal as exc:
            job.state, job.text = "refused", exc.text

    async def _ask(self, aid: str, combo: Combo, prompt: str, suite_task: str, timeout_s: float) -> Reply:
        task = Task(task_id=f"{aid}:{suite_task}", task_type=combo.task_type, sensitivity="public", entry_chat=EntryChat(kind="local", name="admission"), created_by="OWNER", payload_ref="admission")
        route = Route(environment_id=combo.environment_id, subtype=self.envs[combo.environment_id]["subtype"], model_id=combo.model_id, access_id=combo.access_id, machine_id=combo.machine_id)
        t0 = time.monotonic()
        handle = self.router.run_prompt(task, route, prompt)
        if isinstance(handle, Refusal):
            raise RunnerAborted(handle.code)
        rec = self.coord.tasks.tasks[handle.node_task_id]
        try:
            await asyncio.wait_for(rec.done.wait(), timeout_s + 60)
        except TimeoutError:
            return Reply("", time.monotonic() - t0, timed_out=True)
        if rec.status == "done":
            return Reply(str(rec.result.get("output_text", "")), time.monotonic() - t0)
        if rec.reason in ("node_offline", "no_owner_origin", "busy", "sandbox_missing", "not_installed", "access_missing", "unknown_environment", "stale_recipe"):
            raise RunnerAborted(rec.reason)  # the exam's problem, not the model's: nothing is counted
        return Reply("", time.monotonic() - t0, timed_out=rec.status == "timed_out")  # the environment failed on this prompt: the model's failure, counted as one

    # ---- the draft: the whole network's setup, proposed by the agent from what it sees, corrected by the owner's words, approved by his click ---------------------------------------------

    def draft(self, ch, *, action: str, text: str | None = None, run_id: str | None = None) -> dict:
        """start: the agent drafts the network setup; correct: the owner's words (recorded as his) make it draft again; approve: his own system dialog turns the proposed draft into the one in force."""
        via = f"chat:{ch.name}"
        if action in ("start", "correct"):
            if action == "correct" and not (isinstance(text, str) and text.strip()):
                return self._out(False, S.DRAFT_NEEDS_TEXT)
            view = self.coord.start_draft_run(via, correction=" ".join(text.split())[:600] if action == "correct" else None)
            if view.get("status") == "no_model":
                return self._out(False, S.DRAFT_NO_MODEL.format(reason=view.get("reason", "")), run_id=view["id"])
            return self._out(True, S.DRAFT_STARTED.format(run_id=view["id"]), run_id=view["id"])
        if action == "approve":
            cur = self.coord.runs.drafts.current()
            if not cur or cur.get("status") != "proposed" or (run_id and cur.get("run") != run_id):
                return self._out(False, S.DRAFT_NOTHING_PROPOSED)
            plan_hash = "sha256:" + _digest(cur)
            pending = self.coord.harness.request_confirmation(None, summary=S.DRAFT_APPROVE_SUMMARY.format(machines=len(cur.get("machines", []))), plan_hash=plan_hash,
                                                              command="make this the draft in force (it opens or closes subtypes per machine for the router)", undo="the owner can correct it and approve a new round")
            pending.future.add_done_callback(lambda f, rid=cur["run"]: f.result() and self.coord.approve_draft(rid, "owner"))
            return self._out(True, S.DRAFT_ASK.format(intent_id=pending.intent.id), intent_id=pending.intent.id, run_id=cur["run"])
        return self._out(False, S.DRAFT_BAD_ACTION)

    # ---- what has been measured: every admission fact for a task type, ranked by the written rule's own order, and nothing else --------------------------------------------------

    def measurements(self, ch, *, task_type: str) -> dict:
        """The admission facts of one task type, one row per combination: the pass rate and median time that were MEASURED here, whether the measurement is still fresh, and the combinations that are
        candidates but have no measurement (listed, never ranked). The recommendation is the first row that is fresh and admitted; no number that was not measured appears."""
        import statistics
        from envrec_coordinator.admission.facts import FACT_EVENT
        prefix = f"admission.{task_type}."
        served = {m.machine_id: m.model_id for m in self.facts.machines()}
        latest: dict[str, dict] = {}
        for e in self.store.log.events(FACT_EVENT):
            k = str(e.payload.get("fact", ""))
            if k.startswith(prefix):
                latest[k] = e.payload["value"]
        rows = []
        for k, v in latest.items():
            machine, env, *mid, access = k[len(prefix):].split(".")
            model = ".".join(mid)
            route_key = "|".join((env, model, access, machine))
            secs = [r["seconds"] for r in v.get("results", []) if r.get("seconds") is not None]
            uses_local = E.uses_local_endpoint(self.envs[env], access) if env in self.envs else False
            now_served = (not uses_local) or served.get(machine) == model  # an API, or the local model that is being served now
            fresh = self.facts.identity(route_key, task_type) == v.get("identity", {}) if now_served else None  # a model that is not served now cannot be compared with anything: its measurement is kept as it was
            admitted = v["passed"] >= self.policy.threshold
            state = "stale" if fresh is False else ("measured" if admitted else "not_admitted")
            rows.append({"route": route_key, "passed": v["passed"], "of": v["of"], "median_s": round(statistics.median(secs), 3) if secs else None, "fresh": fresh, "served_now": now_served, "admitted": admitted, "state": state})
        rows.sort(key=lambda r: (r["state"] == "stale", not r["admitted"], -r["passed"] / r["of"], r["median_s"] if r["median_s"] is not None else 1e9))
        probe = Task(task_id="adm-probe", task_type=task_type, sensitivity="public", entry_chat=EntryChat(kind="local", name="admission"), created_by="OWNER", payload_ref="measurements")
        cands, _ = build_candidates(probe, self.envs, self.facts, self.policy, {})
        measured_keys = {r["route"] for r in rows}
        unmeasured = [c.route.key for c in cands if c.route.key not in measured_keys]
        top = next((r for r in rows if r["state"] == "measured"), None)
        label = {"measured": ", admitted", "not_admitted": ", not admitted", "stale": ", measured against a model or environment that has since changed (stale: not counted)"}
        lines = [f"Measured on {task_type} (pass mark {self.policy.threshold}/{self.policy.of}):"]
        lines += [f"  {i}. {self._key_text(r['route'])}: {r['passed']}/{r['of']}" + (f", median {r['median_s']} s per task" if r["median_s"] is not None else "") + label[r["state"]] + ("" if r["served_now"] else " (not served now)")
                  for i, r in enumerate(rows, 1)] or ["  nothing has been measured yet."]
        if unmeasured:
            lines.append("Not measured (listed, never ranked): " + "; ".join(self._key_text(k) for k in unmeasured) + ".")
        lines.append(f"Recommendation from measured numbers only: {self._key_text(top['route'])} ({top['passed']}/{top['of']}{', median ' + str(top['median_s']) + ' s' if top['median_s'] is not None else ''})" + ("" if top["served_now"] else "; it is not served now, so serve it first") + "." if top else "Recommendation: none, no combination is measured and admitted.")
        return self._out(True, "\n".join(lines), task_type=task_type, rows=rows, unmeasured=unmeasured, recommendation=top["route"] if top else None)

    # ---- access: add or remove a key on a machine ------------------------------------------------------------------------------------------------

    def _access_name(self, access: str, variable: str | None) -> tuple[str | None, str | None]:
        """(credential name, problem) for an api_key access from the catalog."""
        for e in self.envs.values():
            a = E.access_of(e, access)
            if a is not None and a.get("kind") == "api_key":
                names = [*a.get("env_names", []), *a.get("env_names_any", [])]
                if variable:
                    return (variable, None) if variable in names else (None, S.ACCESS_NEEDS_VARIABLE.format(access=access, names=", ".join(names)))
                return (names[0], None) if len(names) == 1 else (None, S.ACCESS_NEEDS_VARIABLE.format(access=access, names=", ".join(names)))
        return None, S.ACCESS_NO_ACCESS.format(access=access)

    def add_access(self, ch, *, node: str, access: str, variable: str | None = None) -> dict:
        return self._access(ch, node, access, variable, "set")

    def remove_access(self, ch, *, node: str, access: str, variable: str | None = None, undo: bool = False) -> dict:
        return self._access(ch, node, access, variable, "restore" if undo else "remove")

    def _access(self, ch, node: str, access: str, variable: str | None, action: str) -> dict:
        nid = self.coord.harness._resolve_node(node)
        if nid is None:
            return self._out(False, f"{node} is not one of your paired machines.")
        name, problem = self._access_name(access, variable)
        if name is None:
            return self._out(False, problem)
        label = self._name(nid)
        if nid not in self.coord._sessions:
            return self._out(False, S.ACCESS_OFFLINE.format(node=label))
        job_id = "acc-" + secrets.token_hex(4)
        plan_hash = T.credential_plan_hash(nid, access, name, action)
        self.coord.log.add("credential_plan", {"plan_hash": plan_hash, "node": nid, "access": access, "name": name, "action": action, "job": job_id, "tag": "GUARD"})
        self._job(job_id, "access", "checking", data={"node": nid, "access": access, "name": name, "action": action})
        asyncio.ensure_future(self._access_flow(ch, job_id, nid, label, access, name, action, plan_hash))
        return self._out(True, f"Checking {label}. Call mas_job with job_id {job_id} for the approval step.", job_id=job_id)

    async def _access_flow(self, ch, job_id: str, nid: str, label: str, access: str, name: str, action: str, plan_hash: str) -> None:
        job, h = self.jobs[job_id], self.coord.harness
        try:
            if action == "set":  # a key needs a real credential store on that machine, and the owner is told BEFORE the window opens, not after he typed
                probe = self.coord.tasks.start(nid, "credential_presence", {"names": [name]}, by="GUARD")
                await asyncio.wait_for(probe.done.wait(), 20)
                if probe.status != "done":
                    job.state, job.text = "refused", S.ACCESS_OFFLINE.format(node=label) if probe.reason == "node_offline" else S.ACCESS_FAILED.format(reason=probe.reason or probe.status)
                    return
                if not probe.result.get("store_ok"):
                    job.state, job.text = "refused", S.ACCESS_STORE_MISSING.format(node=label)
                    job.data["reason"] = "credential_store_missing"
                    return
                summary = S.ACCESS_ADD_SUMMARY.format(name=name, node=label, access=access)
                pending = h.request_secret_entry(None, summary=summary, plan_hash=plan_hash, command=S.ACCESS_ADD_COMMAND.format(name=name, node=label), undo=S.ACCESS_UNDO_ADD, credential=name, raise_for=ch)
            else:
                summary = (S.ACCESS_REMOVE_SUMMARY if action == "remove" else S.ACCESS_RESTORE_SUMMARY).format(name=name, node=label, access=access)
                pending = h.request_confirmation(None, summary=summary, plan_hash=plan_hash, command=(S.ACCESS_REMOVE_COMMAND if action == "remove" else S.ACCESS_RESTORE_COMMAND).format(name=name, node=label),
                                                 undo=S.ACCESS_UNDO_REMOVE if action == "remove" else S.ACCESS_UNDO_RESTORE, raise_for=ch)
            job.intent_id, job.state, job.text = pending.intent.id, "awaiting_owner", S.ACCESS_PENDING.format(summary=summary, intent_id=pending.intent.id, job_id=job_id)
            ok = await asyncio.wait_for(pending.future, 600)
            if not ok:
                job.state, job.text = "declined", S.ACCESS_DECLINED
                return
            value = h.take_secret(pending.intent.id) if action == "set" else None
            rec = self.coord.tasks.start_credential_step(nid, name, action, plan_hash, value)
            value = None  # the one sealed task has it now; this coroutine does not keep it
            await asyncio.wait_for(rec.done.wait(), 60)
            if rec.status != "done":
                job.state, job.text = "failed", S.ACCESS_FAILED.format(reason=rec.reason or rec.status)
                job.data["reason"] = rec.reason
                return
            names = self.facts.credential_names.setdefault(nid, set())
            (names.add if action in ("set", "restore") else names.discard)(name)
            job.state, job.text = "done", {"set": S.ACCESS_DONE_SET, "remove": S.ACCESS_DONE_REMOVE, "restore": S.ACCESS_DONE_RESTORE}[action].format(name=name, node=label)
        except TimeoutError:
            job.state, job.text = "timed_out", S.ACCESS_FAILED.format(reason="no answer in time")
        except Exception as exc:  # noqa: BLE001
            job.state, job.text = "failed", S.ACCESS_FAILED.format(reason=type(exc).__name__)

    # ---- the board: a goal broken into subtasks by a director, each routed by the router, one approval of the plan ----------------------------------------------------

    def board_start(self, ch, *, goal: str, sensitivity: str) -> dict:
        from envrec_board.board import Board
        from envrec_board.decisions import DecisionDesk
        from envrec_board.log import JsonlLog
        from envrec_board.team import TeamPolicy, TeamRun
        from envrec_coordinator.board_wiring import LoopRouterPort, ModelDirector
        chat = self._chat(ch)
        if not isinstance(goal, str) or not goal.strip() or len(goal) > MAX_TEXT:
            return self._out(False, S.BOARD_REFUSED.format(reason=f"the goal must be 1 to {MAX_TEXT} characters"))
        probe = Task(task_id="board-probe", task_type="text", sensitivity=sensitivity if isinstance(sensitivity, str) else None, entry_chat=chat, created_by="OWNER", payload_ref="goal")
        from envrec_coordinator.router.sensitivity import creation_refusal
        bad = creation_refusal(probe)
        if bad is not None:
            self.coord.log.add("task_refused", {"board": True, "refusal": bad.code, "channel": ch.name, "tag": "GUARD"})
            return self._out(False, S.BOARD_REFUSED.format(reason=bad.reason), refusal={"code": bad.code, "reason": bad.reason})
        loop = asyncio.get_running_loop()
        board_id = "board-" + secrets.token_hex(4)
        board = Board(JsonlLog(self.coord.state / "boards" / f"{board_id}.jsonl"), time.time)
        desk = DecisionDesk(board)
        root = board.post_task("goal", goal, "done", "owner", sensitivity, "owner", actor="GUARD")  # recorded by code on the owner's request: a tool call is never booked as the owner's
        port = LoopRouterPort(self, loop)
        team = TeamRun(board, desk, port, root_id=root, entry_chat=chat.model_dump(), policy=TeamPolicy(preapproved_escalations=self.board_preapproved))
        director = self.director_factory(board_id) if self.director_factory else ModelDirector(self, loop)
        self.boards[board_id] = {"board": board, "desk": desk, "team": team, "director": director, "root": root, "sensitivity": sensitivity, "chat": chat, "state": "running", "intents": {}}
        self._job(board_id, "board", "running", sensitivity=sensitivity)
        asyncio.ensure_future(self._board_run(board_id))
        return self._out(True, S.BOARD_STARTED.format(board_id=board_id, sensitivity=sensitivity), board_id=board_id, job_id=board_id)

    async def _board_run(self, board_id: str) -> None:
        b, job = self.boards[board_id], self.jobs[board_id]
        b["state"] = job.state = "running"
        try:
            out = await asyncio.to_thread(b["team"].run, b["director"])
        except Exception as exc:  # noqa: BLE001
            b["state"] = job.state = "failed"
            job.text = f"The board stopped: {type(exc).__name__}."
            return
        b["state"] = job.state = "waiting_for_owner" if out["state"] == "waiting_for_owner" else out.get("stop") or "stopped"
        job.text = S.BOARD_STATE.format(board_id=board_id, state=job.state)

    def board_show(self, ch, *, board_id: str) -> dict:
        from envrec_board.team import render_view
        b = self.boards.get(board_id)
        if b is None:
            return self._out(False, S.BOARD_UNKNOWN.format(board_id=board_id))
        if b["sensitivity"] == "confidential" and ch.cloud:
            return self._out(True, S.BOARD_WITHHELD, board_id=board_id, state=b["state"])
        board, desk = b["board"], b["desk"]
        with board.reading():  # the board runs in a worker thread: read it as one state, never while a task is being added (a dictionary that changes size under a reader is a 500)
            pending = desk.pending()
            lines = [S.BOARD_STATE.format(board_id=board_id, state=b["state"]), S.BOARD_LIMIT.format(n=self.board_preapproved), render_view(board, b["root"], b["team"].tools.view)]
            lines += [S.BOARD_PENDING.format(decision_id=d["decision_id"], kind=d["kind"], summary=d["summary"], board_id=board_id) for d in pending]
            subs = b["team"].tools.view.subtasks()
            data = {"board_id": board_id, "state": b["state"], "pending": [{"decision_id": d["decision_id"], "kind": d["kind"], "summary": d["summary"]} for d in pending],
                    "subtasks": [{"id": s["task_id"], "type": s["task_type"], "status": s["status"], "route": (s["route"] or {}).get("route", {}).get("environment_id") if isinstance(s["route"], dict) else None,
                                  "route_refusal": (s["route_refusal"] or {}).get("code") if s["route_refusal"] else None, "escalation_step": s.get("escalation_step")} for s in subs]}
        return self._out(True, "\n".join(lines), **data)

    def board_approve(self, ch, *, board_id: str, decision_id: str) -> dict:
        b = self.boards.get(board_id)
        if b is None:
            return self._out(False, S.BOARD_UNKNOWN.format(board_id=board_id))
        d = next((x for x in b["desk"].pending() if x["decision_id"] == decision_id), None)
        if d is None:
            return self._out(False, S.BOARD_NO_DECISION.format(decision_id=decision_id, board_id=board_id))
        if b["sensitivity"] == "confidential" and ch.cloud:
            return self._out(False, S.BOARD_WITHHELD)
        plan_hash = d["hash"]
        pending = self.coord.harness.request_confirmation(None, summary=d["summary"], plan_hash=plan_hash, command="approve exactly this on the board", undo="the board shows what it did and how to stop it",
                                                          sensitivity=b["sensitivity"])
        pending.future.add_done_callback(lambda f, bid=board_id, did=decision_id, ph=plan_hash: self._board_decided(bid, did, ph, f.result()))
        return self._out(True, S.BOARD_ASK.format(summary=d["summary"], intent_id=pending.intent.id), board_id=board_id, decision_id=decision_id, intent_id=pending.intent.id)

    def _board_decided(self, board_id: str, decision_id: str, plan_hash: str, ok: bool) -> None:
        b = self.boards[board_id]
        b["desk"].answer(decision_id, "approve" if ok else "reject", channel=self._confirmation_mechanism(plan_hash) or "panel", by="owner")  # the gesture the ticket recorded: only a person's dialog counts
        asyncio.ensure_future(self._board_run(board_id))

    # ---- the one door the harness core calls ---------------------------------------------------------------------------------------------------------

    SPECS_FILE = E.SKILLS_DIR / "harness-tools" / "references" / "tools.json"

    def specs(self) -> list[dict]:
        """The tools a chat may call, as data (`skills/harness-tools`): name, description, arguments. The adapters list exactly these."""
        return json.loads(self.SPECS_FILE.read_text(encoding="utf-8"))["tools"]

    TOOLS = ("create_task", "explain_route", "run_admission", "job_status", "add_access", "remove_access", "board_start", "board_show", "board_approve", "measurements", "draft")

    def call(self, ch, name: str, args: dict) -> dict:
        """The tool door: a registered tool, its arguments as the chat sent them. The channel is the token's, and `args` can never carry the entry chat."""
        if name not in self.TOOLS:
            return self._out(False, f"There is no tool {name}.")
        fn = getattr(self, name)
        names = fn.__code__.co_varnames[2:fn.__code__.co_argcount + fn.__code__.co_kwonlyargcount]
        try:
            return fn(ch, **{k: v for k, v in (args or {}).items() if k in names})
        except TypeError:
            return self._out(False, f"{name} was called with arguments it does not take.")

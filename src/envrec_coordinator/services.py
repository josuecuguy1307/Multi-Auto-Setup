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
from pathlib import Path
from typing import Any

from envrec_coordinator import recommend_strings as RC
from envrec_coordinator import services_strings as S
from envrec_coordinator import chain_strings as CS
from envrec_coordinator.chain import ChainTools, load_kinds
from envrec_coordinator.judge import JudgeTools
from envrec_coordinator.model_lists import ModelLists
from envrec_coordinator.measure_tools import MeasureTools
from envrec_coordinator.model_list_tools import ModelListTools
from envrec_coordinator.tool_install import ToolInstallTools
from envrec_coordinator.profile_tools import ProfileTools
from envrec_coordinator.recommend import RecommendTools, normalize_domain
from envrec_coordinator.admission import router_bridge as bridge
from envrec_coordinator.admission.decision import AdmissionDesk
from envrec_coordinator.admission.decision import Refusal as AdmissionRefusal
from envrec_coordinator.admission.exam import ExamAborted, Reply, RunnerAborted
from envrec_coordinator.admission.facts import AdmissionFacts, Combo
from envrec_coordinator.admission.suite import SuiteRefused, load_suite
from envrec_coordinator.router import EntryChat, MemoryOutputs, Policy, Refusal, Route, RouteDecision, Router, Task, filter_outbound
from envrec_coordinator.router import grading
from envrec_coordinator.router.catalog import build_candidates, knows_task_type
from envrec_coordinator.router.model import Exclude
from envrec_coordinator.live_facts import CoordinatorFacts
from envrec_net import envrun as E
from envrec_net import task as T

BUSY_RETRIES = 6  # an exam prompt that the node turned away as busy is asked again this many times before the exam is given up as the machine's problem
BUSY_WAIT_S = 3.0
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


class Services(ProfileTools, RecommendTools, ChainTools, JudgeTools, ToolInstallTools, ModelListTools, MeasureTools):
    def __init__(self, coord, *, envs: dict[str, dict] | None = None, policy: Policy | None = None, sandbox_ready=None, sandbox_runner=None) -> None:
        self.coord = coord
        self.rejected_leads: dict[str, list[str]] = {}
        self.intent_jobs: dict[str, str] = {}  # a question's intent id -> the job (or chain) whose next step it belongs to, so an answer can say what it started
        self.chains: dict[str, dict] = {}  # id -> the state of a chain (skill harness-chains)
        self.held: dict[str, dict] = {}  # id -> an input the HARNESS holds (a folder listing): its text, the names to mask, what the chat is told. Never in a tool result
        self.recommend_runs: dict[str, dict] = {}  # id -> the store, adapter and researcher of a recommendation run (protocol-recommend)
        if envs is not None:
            self.catalog = envs
        else:  # the shipped entries and the leads research recorded in the state dir, merged by one loader and held to one guard; the router sees only what can be run
            self.catalog, self.rejected_leads = E.load_catalog(extra_dir=Path(coord.state) / E.RESEARCHED_DIR_NAME)
        self.envs = E.runnable(self.catalog) if envs is None else envs
        self.policy = policy or Policy.defaults()
        self._loop_log = LoopLog(coord.log)
        self.store = AdmissionFacts(self._loop_log, coord.clock)
        self.facts = CoordinatorFacts(coord, self.store, self.envs)
        self.model_lists = self.facts.model_lists = ModelLists(Path(coord.state) / "model_lists")  # the models an API lists, read through its node, kept dated
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
        self._init_profile()  # the owner's profile reads into the router's policy before anything is routed
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

    TERMINAL = ("done", "aborted", "refused", "declined", "failed", "exhausted", "route_changed", "unchecked", "done_unchecked", "task_created", "cancelled")

    def job_line(self, intent_id: str) -> str:
        """What an approved (or answered) question started or belongs to, in one line a chat can follow with mas_job: `job id for mas_job: <id> (<kind>: <state>)`. A task, an exam, an access change and a chain
        are jobs; a setup, an undo or a connection is a run, and mas_job reads a run's id too."""
        jid = self.intent_jobs.get(intent_id)
        job = self.jobs.get(jid) if jid else next((j for j in self.jobs.values() if j.intent_id == intent_id), None)
        if job is not None:
            return S.JOB_LINE.format(job_id=job.id, kind=job.kind, state=job.state)
        i = self.coord.harness._find(intent_id)
        run = self.coord.runs.runs.get(i.run_id) if i is not None and i.run_id else None
        return S.JOB_LINE.format(job_id=run.id, kind=f"{run.kind} run", state=run.status) if run is not None else ""

    def open_lines(self) -> list[str]:
        """The exams, tasks, chains and runs that are open or going now, one line each (mas_activity)."""
        out = [f"{j.kind} {j.id}: {j.state}" for j in self.jobs.values() if j.state not in self.TERMINAL]
        out += [f"{r.kind} run {r.id}: {r.status}" for r in self.coord.runs.runs.values() if r.finished is None]
        return out

    def run_status(self, run_id: str) -> dict | None:
        """A run (analysis, setup, undo, connection, draft) as `mas_job` shows it: its state, the reason it ended, its last steps with who acted, and the question it waits on."""
        run = self.coord.runs.runs.get(run_id)
        if run is None:
            return None
        view = self.coord.runs.view(run.id) or {}
        lines = [S.JOB_STATE.format(job_id=run.id, kind=f"{run.kind} run", state=run.status)]
        if run.reason:
            lines.append(run.reason)
        lines += [f"[{e['tag']}] {e['text']}" for e in view.get("events", [])[-6:]]
        waiting = [x for x in self.coord.harness._logs.get(run.id, []) if x.extra.get("state") == "pending" and x.kind in ("confirm", "ask_choice")]
        for x in waiting:
            lines.append(f"Waiting for you: {x.text_fallback.splitlines()[0][:160]} (intent_id {x.id})")
        return {"text": "\n".join(lines), "state": run.status, "kind": run.kind, "waiting": [x.id for x in waiting]}

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
                "left_out": dict(res.left_out), "filters": trace, "chosen": res.route.key, "cost_class": costs.get(res.route.key), "evidence": res.evidence_label, "evidence_line": res.evidence.get("text", ""), "evidence_tier": res.evidence.get("tier"), "provisional": res.provisional,
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
            if data.get("evidence_line"):
                lines.append(data["evidence_line"][0].upper() + data["evidence_line"][1:] + ".")
            lines.append(self.facts_age_line(data["chosen"].split("|")[-1]))  # the facts this choice rests on, dated: nothing is presented as fresher than it is
        return "\n".join(lines)

    @staticmethod
    def _age(seconds: float) -> str:
        s = max(0, int(seconds))
        return f"{s} s" if s < 90 else f"{s // 60} min" if s < 5400 else f"{round(s / 3600)} hours" if s < 172800 else f"{round(s / 86400)} days"

    def facts_age_line(self, machine_id: str) -> str:
        """How old what a route rests on is: the stored analysis of the machine and the last check of its key names. The facts are not refreshed here (an analysis takes minutes); they are dated."""
        doc = self.coord.runs.stored_analysis(machine_id)
        creds = self._creds_at.get(machine_id)
        age_c = self._age(time.monotonic() - creds) if creds is not None else S.FACTS_UNKNOWN
        if doc is None or "stored_at" not in doc:
            return S.FACTS_NO_ANALYSIS.format(machine=self._name(machine_id), creds=age_c)
        return S.FACTS_AGE.format(machine=self._name(machine_id), age=self._age(time.time() - float(doc["stored_at"])), creds=age_c)

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
        proofs = [a["proof"]["line"] for a in (self.tasks.get(task_id) or {}).get("attempts", []) if a.get("proof")] if task_id else []
        return self._out(True, self._explain_text(data) + ("".join("\n" + x for x in proofs)), **data, **({"proofs": proofs} if proofs else {}))

    def create_task(self, ch, *, text: str, task_type: str, touches=None, check: dict | None = None, sensitivity: str | None = None, domain=None, inputs=None) -> dict:
        """Create a task. Its level comes from the owner's profile and what the text touches (`touches`: his categories, `none` or `unsure`); when the harness cannot tell it asks him. A chat's model never gives a
        level: only the owner's own screens (panel, terminal) state one. Nothing runs: the owner approves the exact route first."""
        if sensitivity is not None:
            if ch.model_in_middle:
                return self._out(False, S.TASK_LEVEL_NOT_YOURS)
            return self._create(ch, text=text, task_type=task_type, sensitivity=sensitivity, check=check, domain=domain, inputs=inputs)
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
            return self._create(ch, text=text, task_type=task_type, sensitivity="internal", check=check)  # the refusal for the size is the same one, with the size in it
        cats = [t for t in (touches if isinstance(touches, list) else []) if isinstance(t, str) and t in self.profile.categories()]  # the owner's own category names: the only words a confidential request may research with
        kinds = load_kinds()
        eff = list(touches) if isinstance(touches, list) else ([] if touches is None else [touches])
        if inputs:  # what the harness will read also touches the owner's categories (data, per kind): the level is derived from all of them
            eff += [c for i in inputs if isinstance(i, dict) for c in kinds.get(i.get("kind"), {}).get("categories", [])]
        level, res = self._level_or_question(ch, text, task_type, eff, check, domain, cats, inputs)
        return res if level is None else self._create(ch, text=text, task_type=task_type, sensitivity=level, check=check, domain=domain, categories=cats, inputs=inputs)

    def _create(self, ch, *, text: str, task_type: str, sensitivity: str, check: dict | None = None, domain=None, only_env: str | None = None, categories=None, inputs=None, held: dict | None = None, offered: bool = False) -> dict:
        self._stale_credentials()
        task_id = "t-" + secrets.token_hex(4)
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
            return self._out(False, S.TASK_REFUSED.format(task_id=task_id, reason=S.TEXT_SIZE.format(max=MAX_TEXT, got=len(text) if isinstance(text, str) else 0)))
        spec = None
        if check is not None:
            kind = check.get("kind") if isinstance(check, dict) else None
            if kind not in CHECK_KINDS or ("files" if kind == "project_tests" else "value") not in check:
                return self._out(False, S.TASK_REFUSED.format(task_id=task_id, reason=f"a check is one of {', '.join(CHECK_KINDS)}, graded by code"))
            if len(json.dumps(check, default=str)) > MAX_CHECK:
                return self._out(False, S.TASK_REFUSED.format(task_id=task_id, reason="the check is too large"))
            spec = dict(check)
        if inputs and held is None and not (ch.cloud and set(categories or []) & set(self.profile.get("confidential"))):  # a cloud chat that itself says the task is confidential still gets the router's refusal; otherwise the harness carries the inputs
            return self.chain_task(ch, text=text, task_type=str(task_type), sensitivity=sensitivity, check=check, domain=domain, categories=categories, inputs=inputs)
        if only_env is None and held is None and not knows_task_type(self.envs, str(task_type)) and sensitivity in ("public", "internal", "confidential"):
            return self.recommend(ch, text=text, description=str(task_type), domain=domain, sensitivity=sensitivity, check=check, categories=categories)  # no catalog type names this work: protocol-recommend, not a dead end
        tags = normalize_domain(domain, None) if domain else []
        if tags is None:
            return self._out(False, S.TASK_REFUSED.format(task_id=task_id, reason=RC.REC_BAD_DOMAIN))
        if not offered and only_env is None and held is None:
            asked = self._clearance_offer(ch, args={"text": text, "task_type": task_type, "sensitivity": sensitivity, "check": check, "domain": domain, "categories": categories, "inputs": inputs}, task_type=str(task_type)[:40], sensitivity=sensitivity, tags=tags)
            if asked is not None:
                return asked
        chat = self._chat(ch)
        ref = f"payload:{task_id}"
        self.payloads[ref] = text
        others = [r.node_id for r in self.coord.registry.all() if held and r.node_id != held["machine"]]  # a task on a computer the owner named: every other computer is out
        pin = Exclude(environment_ids=sorted(k for k in self.envs if k != only_env)[:64] if only_env else [], machine_ids=others[:64]) if (only_env or others) else None  # the owner chose this environment: the rule still picks the access and the machine
        task = Task(task_id=task_id, task_type=str(task_type)[:40], sensitivity=sensitivity if isinstance(sensitivity, str) else None, entry_chat=chat, created_by="OWNER", payload_ref=ref, domain=tags, exclude=pin,
                    held_inputs=list(held["ids"]) if held else [])
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
            command=self._run_command(res, bool(spec)), undo="nothing to undo: it works in a folder that is deleted when it ends", sensitivity="internal" if held else res.effective_sensitivity, kind="task_run")  # the question names the route, never the held input
        self.tasks[task_id] = {"task": task, "check": spec, "plan_hash": plan_hash, "approved_route": res.route.key, "attempts": [], "intent_id": pending.intent.id,
                               "mask": sorted({n for h in (held["ids"] if held else []) for n in self.held[h]["names"]}, key=len, reverse=True), "answer_travels": bool(held) and all(self.held[h]["answer_travels"] for h in held["ids"])}
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
        return self._out(True, S.TASK_CREATED.format(task_id=task_id, task_type=task.task_type, sensitivity=res.effective_sensitivity, route_line=route_line, intent_id=pending.intent.id, answer=self.coord.harness.answer_how(pending.intent.id), what=self._run_command(res, bool(spec)), check_line=check_line)
                         + "\n" + S.TEXT_RECEIVED.format(n=len(held["chat_text"] if held else text), tail=" ".join((held["chat_text"] if held else text).split())[-50:]) + "\n" + self._explain_text(data), task_id=task_id, intent_id=pending.intent.id, **data)

    def _run_command(self, res: RouteDecision, has_check: bool) -> str:
        """What the owner is told runs, where, and how many calls it can make (the question's text): the route, its place (a computer, or the cloud), the calls, and what a failed check may add."""
        entry = self.envs[res.route.environment_id]
        where = f"on {self._name(res.route.machine_id)}, " + ("leaving your computers (cloud)" if E.is_cloud(entry, res.route.access_id) else "staying on your computers")
        more = f" If its code check fails the rule may try up to {self.policy.max_escalation} more routes, each one a further call that you are told of." if has_check else " No code check was given, so there is one call and nothing escalates."
        return f"1 call to {self._key_text(res.route.key)}, {where}, in a folder deleted afterwards (evidence: {res.evidence_label}{', provisional' if res.provisional else ''}).{more}"

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
                    attempt["proof"] = self._proof_of(rec.result)
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

    @staticmethod
    def _mask(text: str, names: list[str]) -> str:
        """Every name from a held listing, wherever the answer repeats it (any case), replaced: the longest first, so a name inside a longer one is not left half visible."""
        import re
        for n in names:
            text = re.sub(re.escape(n), CS.WITHHELD, text, flags=re.I)
        return text

    @staticmethod
    def _proof_of(result: dict) -> dict:
        """What the node recorded about a run, checked by the coordinator before it is shown (it never writes one): {ok, why, line, summary}. `summary` is the verified part that may be kept in an event."""
        proof = result.get("proof")
        ok, why = E.verify_proof(proof, str(result.get("output_text", "")))
        api = (proof.get("api") or {}) if ok else {}
        return {"ok": ok, "why": why, "line": E.proof_line(proof or {}, ok, why),
                "summary": ({"node_host": proof["node_host"], "started_at": proof["started_at"], "ended_at": proof["ended_at"], "response_id": api.get("response_id"), "model_returned": api.get("model"),
                             "model_requested": proof.get("model_requested"), "usage": api.get("usage"), "bytes_sent": proof["bytes_sent"], "bytes_received": proof["bytes_received"]} if ok else None)}

    def job_status(self, ch, job_id: str) -> dict:
        job = self.jobs.get(job_id)
        if job is None:
            rs = self.run_status(job_id)
            return self._out(True, rs["text"], job_id=job_id, state=rs["state"], waiting=rs["waiting"]) if rs is not None else self._out(False, S.STATUS_UNKNOWN.format(job_id=job_id))
        lines = [S.JOB_STATE.format(job_id=job.id, kind=job.kind, state=job.state)]
        if job.text:
            lines.append(job.text)
        if job.intent_id and job.state in ("awaiting_approval", "awaiting_owner"):
            lines.append(f"Waiting for you: {self.coord.harness.answer_how(job.intent_id)}.")
        st = self.tasks.get(job.id)
        if st:
            for a in st["attempts"]:
                lines.append(f"Step {a['step']}: {self._key_text(a['route'])}: {a['state']}" + (f" ({a['reason']})" if a["reason"] else "") + (f", code check {'passed' if a['graded'] else 'failed'}" if isinstance(a["graded"], bool) else ""))
                if a.get("proof"):
                    lines.append("  " + a["proof"]["line"])
        data = {k: v for k, v in job.data.items() if k != "output"}
        if "output" in job.data and st and st.get("answer_travels"):  # a task whose level came from inputs the harness holds: the ANSWER comes back, with every name from the listing masked, and the route that produced it
            masked = self._mask(job.data["output"], st["mask"])
            route = self._key_text(st["attempts"][-1]["route"]) if st["attempts"] else "the route the owner approved"
            lines.append(CS.ANSWER_HEAD.format(route=route) + "\n" + masked[:6000])
            data["output"] = masked
        elif "output" in job.data:
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
                                                       undo="nothing to undo: it only records a measurement", sensitivity="public", kind="admission")
        self._job(aid, "admission", "awaiting_owner", intent_id=conf.intent.id, sensitivity="public", data={"combo": combo.key, "suite": suite.suite_id, "suite_hash": suite.hash})
        conf.future.add_done_callback(lambda f, a=aid, c=combo, s=suite, ph=plan_hash, chn=ch.name: self._admission_decided(a, c, s, ph, chn, f.result()))
        return self._out(True, S.ADMISSION_ASK.format(text=pending["text"], answer=self.coord.harness.answer_how(conf.intent.id)), job_id=aid, intent_id=conf.intent.id, combo=combo.key)

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
            replies = [e.payload for e in self.coord.log.events("admission_reply") if e.payload.get("fact") == combo.key]  # what the node recorded for each prompt of this exam (verified before it was kept)
            proofs = [p_["proof"] for p_ in replies if p_.get("proof")]
            job.data["node_proofs"] = {"replies": len(replies), "with_proof": len(proofs), "hosts": sorted({p_["node_host"] for p_ in proofs}), "models_returned": sorted({p_["model_returned"] for p_ in proofs if p_.get("model_returned")})}
            job.text += " " + S.ADMISSION_PROOFS.format(with_proof=len(proofs), replies=len(replies), hosts=", ".join(job.data["node_proofs"]["hosts"]) or "none",
                                                        models=", ".join(job.data["node_proofs"]["models_returned"]) or "none reported")
        except ExamAborted as exc:
            job.state, job.text = "aborted", S.ADMISSION_ABORTED.format(environment=combo.environment_id, task_type=combo.task_type, reason=exc.reason)
        except AdmissionRefusal as exc:
            job.state, job.text = "refused", exc.text

    async def _ask(self, aid: str, combo: Combo, prompt: str, suite_task: str, timeout_s: float) -> Reply:
        task = Task(task_id=f"{aid}:{suite_task}", task_type=combo.task_type, sensitivity="public", entry_chat=EntryChat(kind="local", name="admission"), created_by="OWNER", payload_ref="admission")
        route = Route(environment_id=combo.environment_id, subtype=self.envs[combo.environment_id]["subtype"], model_id=combo.model_id, access_id=combo.access_id, machine_id=combo.machine_id)
        t0 = time.monotonic()
        for attempt in range(BUSY_RETRIES + 1):
            handle = self.router.run_prompt(task, route, prompt)
            if isinstance(handle, Refusal):
                if handle.code == "busy" and attempt < BUSY_RETRIES:
                    await asyncio.sleep(BUSY_WAIT_S)  # the node was still finishing something of its own: asking again a moment later is not the model's failure and not a reason to end the exam
                    continue
                raise RunnerAborted(handle.code)
            rec = self.coord.tasks.tasks[handle.node_task_id]
            try:
                await asyncio.wait_for(rec.done.wait(), timeout_s + 60)
            except TimeoutError:
                await self.coord.tasks.cancel(handle.node_task_id)  # the environment is still thinking: stop waiting for it AND free its place on the node, so the next prompt of the exam is not turned away as busy
                return Reply("", time.monotonic() - t0, timed_out=True)
            if rec.reason == "busy" and attempt < BUSY_RETRIES:
                await asyncio.sleep(BUSY_WAIT_S)  # the node was still finishing something of its own: asking again a moment later is not the model's failure and not a reason to end the exam
                continue
            break
        if rec.status == "done":
            return Reply(str(rec.result.get("output_text", "")), time.monotonic() - t0, proof=self._proof_of(rec.result)["summary"])
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
                                                              command="make this the draft in force (it opens or closes subtypes per machine for the router)", undo="the owner can correct it and approve a new round", kind="draft_approve")
            pending.future.add_done_callback(lambda f, rid=cur["run"]: f.result() and self.coord.approve_draft(rid, "owner"))
            return self._out(True, S.DRAFT_ASK.format(answer=self.coord.harness.answer_how(pending.intent.id)), intent_id=pending.intent.id, run_id=cur["run"])
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
        same = next((j for j in self.jobs.values() if j.kind == "access" and j.state in ("checking", "awaiting_owner") and (j.data.get("node"), j.data.get("access"), j.data.get("name"), j.data.get("action")) == (nid, access, name, action)), None)
        if same is not None:  # the same item is already open: no second dialog, and the first is not timed out
            return self._out(True, S.ACCESS_ALREADY_OPEN.format(node=label, name=name, job_id=same.id), job_id=same.id)
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
            if action in ("set", "restore"):  # an access was stored: the harness itself proposes what it enables (from the catalog), as one question
                job.text += "\n\n" + self.proposal_text(ch)
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
                                                          sensitivity=b["sensitivity"], kind="board")
        pending.future.add_done_callback(lambda f, bid=board_id, did=decision_id, ph=plan_hash: self._board_decided(bid, did, ph, f.result()))
        return self._out(True, S.BOARD_ASK.format(summary=d["summary"], answer=self.coord.harness.answer_how(pending.intent.id)), board_id=board_id, decision_id=decision_id, intent_id=pending.intent.id)

    def _board_decided(self, board_id: str, decision_id: str, plan_hash: str, ok: bool) -> None:
        b = self.boards[board_id]
        b["desk"].answer(decision_id, "approve" if ok else "reject", channel=self._confirmation_mechanism(plan_hash) or "panel", by="owner")  # the gesture the ticket recorded: only a person's dialog counts
        asyncio.ensure_future(self._board_run(board_id))

    # ---- the one door the harness core calls ---------------------------------------------------------------------------------------------------------

    SPECS_FILE = E.SKILLS_DIR / "harness-tools" / "references" / "tools.json"

    def specs(self) -> list[dict]:
        """The tools a chat may call, as data (`skills/harness-tools`): name, description, arguments. The adapters list exactly these."""
        tools = json.loads(self.SPECS_FILE.read_text(encoding="utf-8"))["tools"]
        accesses = sorted({a["id"] for e in self.envs.values() for a in e.get("accesses", []) if a.get("kind") == "api_key"})  # the choices come from the catalog, never from a model's memory
        types = sorted({t for e in self.envs.values() for t in e.get("task_types", [])})
        for t in tools:
            props = t["inputSchema"]["properties"]
            if t["name"] in ("mas_add_access", "mas_remove_access") and "access" in props:
                props["access"] = {**props["access"], "enum": accesses}
            if t["name"] == "mas_create_task" and "inputs" in props:
                props["inputs"] = {**props["inputs"], "items": {**props["inputs"]["items"], "properties": {**props["inputs"]["items"]["properties"], "kind": {"type": "string", "enum": sorted(load_kinds())}}}}
            if t["name"] == "mas_create_task":
                props["task_type"] = {**props["task_type"], "description": props["task_type"]["description"] + " Catalog types now: " + ", ".join(types) + "."}  # a free description is allowed: it is never squeezed into one of these
                props["touches"] = {**props["touches"], "items": {"type": "string", "enum": [*self.profile.categories(), "none", "unsure"]}}
        return tools

    TOOLS = ("create_task", "explain_route", "run_admission", "job_status", "add_access", "remove_access", "board_start", "board_show", "board_approve", "measurements", "draft", "profile_tool", "propose", "cancel", "install_tool", "refresh_models", "measure_recommender")

    def call(self, ch, name: str, args: dict) -> dict:
        """The tool door: a registered tool, its arguments as the chat sent them. The channel is the token's, and `args` can never carry the entry chat."""
        if name not in self.TOOLS:
            return self._out(False, f"There is no tool {name}.")
        fn = getattr(self, name)
        names = fn.__code__.co_varnames[2:fn.__code__.co_argcount + fn.__code__.co_kwonlyargcount]
        try:
            out = fn(ch, **{k: v for k, v in (args or {}).items() if k in names})
        except TypeError:
            return self._out(False, f"{name} was called with arguments it does not take.")
        if name not in ("profile_tool", "cancel") and out.get("ok") and not out.get("question") and ch.model_in_middle:
            out["text"] = self._more_questions(out["text"], ch)  # first run: the harness itself puts the owner's next profile question
        return out

"""Harness runs on the coordinator: the agent loop for a machine analysis (protocol-analyze) and for pairing offers (protocol-lan).

`AgentRuns.start_analysis` is what the owner's "Analyze this machine" does: it starts a run in which the MODEL picks one read-only probe per turn from a closed menu. If no model is
reachable the run does NOT pretend: it ends as `no_model` with a typed plain reason, and offers the raw probe report from the same probes, run by code in a fixed order and labeled
"code-only, no model" (its events are tagged SCRIPT). Every run's log has an actor count."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import Counter
from pathlib import Path

from envrec.contracts import Actor, Event, FactStatus, TargetKind
from envrec.loop import Loop, RunConfig
from envrec.model import load_profile, make_adapter
from envrec.protocols import ProtocolSet
from envrec.skills.discover import library
from envrec.store import Store
from envrec_coordinator import agent as A
from envrec_coordinator import approval
from envrec_coordinator import draft as DR
from envrec_coordinator import strings as S
from envrec_coordinator.draft_agent import DraftAdapter
from envrec_coordinator.setup_agent import ConnectAdapter, SetupAdapter, UndoAdapter
from envrec_net import task as T

MAX_STEPS = 40
DEFAULT_MODELS_FILE = Path(__file__).resolve().parents[2] / "config" / "models.yaml"  # this program's own file, not one relative to wherever the process was started (a service starts elsewhere)
MAX_SECONDS = 900.0
CODE_ONLY_ORDER = ("os_details", "memory_for_models", "gpus", "model_runtimes", "ai_clis", "installed_programs")
LABEL_CODE_ONLY = S.LABEL_CODE_ONLY


MAX_FALLBACKS = 3  # replies that were not a valid action even after the retry (code had to take a fallback)
MAX_REJECTED = 6  # valid-looking actions that were refused (outside the menu, outside the protocol step, bad arguments)


class HarnessLoop(Loop):
    """The harness loop with one extra rule: repeated invalid actions end the run as `blocked_needs_human` (never as a success, never by code carrying on for the model)."""

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self._invalid = 0
        self._refused = 0

    def _emit_model_event(self, type: str, payload: dict) -> None:
        super()._emit_model_event(type, payload)
        if type == "model_fallback":
            self._invalid += 1
            if self._invalid >= MAX_FALLBACKS:
                self._blocked = True

    def _reject(self, action, reason):  # type: ignore[override]
        self._refused += 1
        if self._refused >= MAX_REJECTED:
            self._blocked = True
        return super()._reject(action, reason)


class AgentRun:
    def __init__(self, run_id: str, kind: str, node_id: str | None, node_name: str, via: str) -> None:
        self.id, self.kind, self.node_id, self.node_name, self.via = run_id, kind, node_id, node_name, via
        self.status = "starting"  # starting | running | done | no_options | no_machines | blocked_needs_human | stopped | no_model | failed
        self.reason = ""
        self.started = time.time()
        self.finished: float | None = None
        self.store: Store | None = None
        self.target = None
        self.adapter: A.BaseAdapter | None = None
        self.task: asyncio.Task | None = None
        self.loop_run_id: str | None = None
        self.summary: dict = {}
        self.result: dict | None = None  # the compiled analysis, when the run ended
        self.code_only: dict | None = None
        self.picked_option: str | None = None
        self.model_status: dict = {}


def _short(v, n: int = 90) -> str:
    s = str(v)
    return s if len(s) <= n else s[: n - 1] + "…"


def describe_event(e: Event) -> str:
    """One line for the owner about one recorded step, in the words of `envrec_coordinator.strings`. Words the model or the machine wrote (its reason, a name) are shown as they are."""
    p = e.payload
    t = e.type
    if t == "action_chosen":
        return S.EVENT[t].format(action=p.get("action"), tool=f" {p.get('tool')}" if p.get("tool") else "", why=f": {_short(p.get('why'))}" if p.get("why") else "")
    if t == "tool_run":
        return S.EVENT[t].format(tool=p.get("tool"))
    if t == "tool_result":
        return f"{p.get('tool')}: " + (S.EVENT["tool_ok"] if p.get("ok", True) else S.EVENT["tool_failed"].format(error=_short(p.get("error")))) + (S.EVENT["tool_facts"].format(facts=p.get("facts")) if p.get("facts") is not None else "")
    if t == "protocol_state":
        return S.EVENT[t].format(from_=p.get("from"), to=p.get("to"), by=p.get("by"))
    if t == "run_requested":
        return S.EVENT[t]
    if t == "owner_answer":
        return S.EVENT[t].format(question=p.get("question"), option=p.get("option")) + (S.EVENT["owner_answer_operator"] if p.get("by") == "claude-code" else "")
    if t == "owner_pick":
        return S.EVENT[t].format(waiting=p.get("waiting"))
    if t == "owner_pick_option":
        return S.EVENT[t].format(option=p.get("option"))
    if t == "owner_confirmation":
        return S.EVENT[t].format(verdict=S.EVENT["approved" if p.get("approved") else "declined"], plan_hash=str(p.get("plan_hash", ""))[:19], mechanism=p.get("mechanism"))
    if t == "owner_correction":
        return f"corrected the draft: {_short(p.get('text'), 120)}" + (" (the operator, for the owner)" if p.get("by") == "claude-code" else "")
    if t == "owner_approve_draft":
        return f"approved the draft of round {p.get('round')}" + (" (the operator, for the owner)" if p.get("by") == "claude-code" else "")
    if t == "run_end":
        return S.EVENT[t].format(stop_reason=p.get("stop_reason"))
    if t in ("action_rejected", "protocol_refused", "policy_decision"):
        return f"{t.replace('_', ' ')}: {_short(p.get('reason') or p.get('decision') or p.get('action'))}"
    if t == "setup_plan":
        return S.EVENT[t].format(steps=p.get("steps"), mib=p.get("download_bytes", 0) // 2 ** 20)
    if t == "setup_step":
        return S.EVENT[t].format(step=p.get("step"), action=p.get("action"), state=str(p.get("state")).replace("_", " "), reason=f" ({p['reason']})" if p.get("reason") else "")
    if t == "code_only_probe":
        return S.EVENT[t].format(probe=p.get("probe"), status=p.get("status"))
    return t.replace("_", " ")


OWNER_GESTURES = ("panel", "panel_click", "os_dialog", "host_dialog", "host_elicitation", "cli_typed", "chat_choice")  # the human gestures that may approve a write; set by trusted code (the owner's panel, the core's confirmation), never by a request body
SHOWN = {"owner_confirmation", "setup_plan", "setup_step", "run_requested", "action_chosen", "tool_run", "tool_result", "protocol_state", "owner_answer", "owner_pick", "owner_pick_option", "owner_correction", "owner_approve_draft", "run_end", "action_rejected", "protocol_refused", "code_only_probe", "run_start"}


class AgentRuns:
    def __init__(self, coord, *, model_profile: str | None = None, models_file: Path | None = None) -> None:
        self.coord, self.model_profile = coord, model_profile
        self.models_file = models_file or DEFAULT_MODELS_FILE
        self.runs: dict[str, AgentRun] = {}
        self.dir = Path(coord.state) / "runs"
        self.analyses = Path(coord.state) / "analyses"  # the finished analysis of each machine, one file per node: what the setup-options protocol reads
        self.setups = Path(coord.state) / "setups"  # what a setup run left on each machine, whole (its plan and where each step stands): what the undo protocol starts from
        self.picks = Path(coord.state) / "picks"  # the option the owner picked for each machine, whole, as it was offered: what the setup protocol starts from

    # ---- the model ----------------------------------------------------------------------------------------------------------------------------

    def model_status(self) -> dict:
        if not self.model_profile:
            return {"available": False, "profile": None, "reason": "no model is configured on the coordinator (start it with --model-profile NAME)"}
        try:
            profile, _ = load_profile(self.models_file, self.model_profile)
        except (KeyError, OSError) as exc:
            return {"available": False, "profile": self.model_profile, "reason": f"the model profile could not be read ({type(exc).__name__})"}
        return A.check_model(profile)

    def _model(self):
        profile, options = load_profile(self.models_file, self.model_profile)
        return make_adapter(profile, options)

    # ---- starting a run ------------------------------------------------------------------------------------------------------------------------

    def _owner_event(self, store, type: str, payload: dict, run_id: str | None) -> None:
        A.write_owner_event(store, type, payload, run_id)

    def _started(self, run: AgentRun) -> None:
        """Who asked for this run. The owner's own press is an OWNER event in the run's log; a start by the operator (CLAUDE-CODE) is counted apart and never as the owner's."""
        if run.via == "harness":  # started by the harness itself (a chain reading a computer): a GUARD record, never an OWNER event
            run.store.append_event(Event(actor=Actor.policy, type="run_requested", payload={"kind": run.kind, "via": "harness"}, run_id=None, tag="GUARD"))
            return
        A.write_owner_event(run.store, "run_requested", {"kind": run.kind, "via": run.via, **({"by": "claude-code"} if run.via == "claude-code" else {})}, None)  # OWNER for the owner's press, CLAUDE-CODE for the operator's

    def start_analysis(self, node_id: str, via: str = "web", *, model=None, status: dict | None = None) -> AgentRun:
        node = self.coord.registry.get(node_id)
        run = AgentRun(uuid.uuid4().hex[:10], "analysis", node_id, node.name if node else node_id, via)
        self.runs[run.id] = run
        self.coord.log.add("analysis_requested", {"run": run.id, "node": node_id, "via": via}, owner=(via not in ("claude-code", "harness")))  # a chain's own read is the harness's, not the owner's press  # the OWNER pressed the button, unless the start says it was the operator (CLAUDE-CODE)
        if node is None:
            run.status, run.reason = "failed", "that computer is not paired"
            return run
        if self.coord._sessions.get(node_id) is None:
            run.status, run.reason = "failed", "the machine is not connected right now"
            return run
        run.model_status = status or self.model_status()
        run.store = Store(self.dir / run.id)
        run.target = A.new_target(run.store, TargetKind.coordinator, node_id)
        self._started(run)
        if not run.model_status["available"] and model is None:
            run.status, run.reason = "no_model", "no model available on the coordinator: " + (run.model_status.get("reason") or "")
            run.task = asyncio.ensure_future(self._code_only(run))
            return run
        adapter = A.AnalysisAdapter(node_id=node_id, node_name=run.node_name, run_task=lambda tt, p: self.coord.tasks.start(node_id, tt, p, by="GUARD", timeout_s=T.TIMEOUTS_S.get(tt)),
                                    owner_events=self._owner_event)
        run.adapter = adapter
        run.task = asyncio.ensure_future(self._execute(run, adapter, model or self._model()))
        return run

    def start_options(self, node_id: str, via: str = "web", *, model=None, status: dict | None = None) -> AgentRun:
        """Start protocol-setup-options for a machine. It reads the STORED analysis: without one, nothing starts (a typed refusal, no model call)."""
        node = self.coord.registry.get(node_id)
        run = AgentRun(uuid.uuid4().hex[:10], "options", node_id, node.name if node else node_id, via)
        self.runs[run.id] = run
        self.coord.log.add("options_requested", {"run": run.id, "node": node_id, "via": via}, owner=(via != "claude-code"))
        if node is None:
            run.status, run.reason = "failed", "that computer is not paired"
            return run
        if self.stored_analysis(node_id) is None:
            run.status, run.reason = "no_analysis", "no stored analysis for this machine: analyse it first"
            run.finished = time.time()
            return run
        run.model_status = status or self.model_status()
        run.store = Store(self.dir / run.id)
        run.target = A.new_target(run.store, TargetKind.setup, node_id)
        self._started(run)
        if not run.model_status["available"] and model is None:
            run.status, run.reason = "no_model", "no model available on the coordinator: " + (run.model_status.get("reason") or "")
            run.finished = time.time()
            return run
        adapter = A.OptionsAdapter(node_id=node_id, node_name=run.node_name, load_stored=lambda: self.stored_analysis(node_id), owner_events=self._owner_event,
                                   prefer_publisher=lambda: self.coord.services.profile.get("preferred_publisher") if getattr(self.coord, "services", None) is not None else None)
        run.adapter = adapter
        run.task = asyncio.ensure_future(self._execute(run, adapter, model or self._model()))
        return run

    def start_install(self, node_id: str, via: str = "web", *, model=None, status: dict | None = None, recipes: dict | None = None, catalog: dict | None = None, rules=None) -> AgentRun:
        """Start protocol-setup for a machine. It needs BOTH the stored analysis and the owner's recorded pick: without either nothing starts (a typed refusal, no model call)."""
        node = self.coord.registry.get(node_id)
        run = AgentRun(uuid.uuid4().hex[:10], "install", node_id, node.name if node else node_id, via)
        self.runs[run.id] = run
        self.coord.log.add("install_requested", {"run": run.id, "node": node_id, "via": via}, owner=(via != "claude-code"))
        if node is None:
            run.status, run.reason = "failed", "that computer is not paired"
        elif self.coord._sessions.get(node_id) is None:
            run.status, run.reason = "failed", "the machine is not connected right now"
        elif self.stored_analysis(node_id) is None:
            run.status, run.reason = "no_analysis", "no stored analysis for this machine: analyse it first"
        elif self.stored_pick(node_id) is None:
            run.status, run.reason = "no_pick", "the owner has not picked an option for this machine: choose one in the options run first"
        if run.status != "starting":
            run.finished = time.time()
            return run
        run.model_status = status or self.model_status()
        run.store = Store(self.dir / run.id)
        run.target = A.new_target(run.store, TargetKind.install, node_id)
        self._started(run)
        if not run.model_status["available"] and model is None:
            run.status, run.reason = "no_model", "no model available on the coordinator: " + (run.model_status.get("reason") or "")
            run.finished = time.time()
            return run
        approve = lambda h, n, a, by: self.coord.log.add("setup_step_approved", {"run": run.id, "node": node_id, "plan_hash": h, "step": n, "action": a, **({"by": "claude-code"} if by != "owner" else {})}, owner=True)  # noqa: E731
        adapter = SetupAdapter(node_id=node_id, node_name=run.node_name, load_stored=lambda: self.stored_analysis(node_id), load_pick=lambda: self.stored_pick(node_id),
                               run_task=lambda tt, p: self.coord.tasks.start(node_id, tt, p, by="GUARD", timeout_s=T.TIMEOUTS_S.get(tt)),
                               start_step=lambda plan, n, action: self.coord.tasks.start_setup_step(node_id, plan, n, action), approve=approve, owner_events=self._owner_event, recipes=recipes, catalog=catalog, rules=rules, plan_approval=approval.mechanism_for("install") if approval.mechanism_for("install") == approval.CHAT_CHOICE else None)
        run.adapter = adapter
        run.task = asyncio.ensure_future(self._execute(run, adapter, model or self._model()))
        return run

    def start_uninstall(self, node_id: str, via: str = "web", *, model=None, status: dict | None = None) -> AgentRun:
        """Start protocol-undo for a machine whose setup left a record. Nothing recorded to undo, or the machine not connected: a typed refusal, no model call."""
        local = node_id.startswith("connect-")  # what a connection of a chat wrote on THIS computer: no paired node, the coordinator's own code undoes it
        node = None if local else self.coord.registry.get(node_id)
        run = AgentRun(uuid.uuid4().hex[:10], "uninstall", node_id, "this computer" if local else node.name if node else node_id, via)
        self.runs[run.id] = run
        self.coord.log.add("uninstall_requested", {"run": run.id, "node": node_id, "via": via}, owner=(via != "claude-code"))
        if node is None and not local:
            run.status, run.reason = "failed", "that computer is not paired"
        elif not local and self.coord._sessions.get(node_id) is None:
            run.status, run.reason = "failed", "the machine is not connected right now"
        elif self.stored_setup(node_id) is None:
            run.status, run.reason = "nothing_to_undo", "this machine has no recorded setup with anything left to undo"
        if run.status != "starting":
            run.finished = time.time()
            return run
        run.model_status = status or self.model_status()
        run.store = Store(self.dir / run.id)
        run.target = A.new_target(run.store, TargetKind.uninstall, node_id)
        self._started(run)
        if not run.model_status["available"] and model is None:
            run.status, run.reason = "no_model", "no model available on the coordinator: " + (run.model_status.get("reason") or "")
            run.finished = time.time()
            return run
        approve = lambda h, n, a, by: self.coord.log.add("setup_step_approved", {"run": run.id, "node": node_id, "plan_hash": h, "step": n, "action": a, **({"by": "claude-code"} if by != "owner" else {})}, owner=True)  # noqa: E731
        adapter = UndoAdapter(node_id=node_id, node_name=run.node_name, load_setup=lambda: self.stored_setup(node_id), start_step=(lambda plan, n, action: self.coord.connect_step(plan, n, action)) if local else (lambda plan, n, action: self.coord.tasks.start_setup_step(node_id, plan, n, action)),
                              approve=approve, owner_events=self._owner_event, plan_approval=approval.mechanism_for("uninstall") if approval.mechanism_for("uninstall") == approval.CHAT_CHOICE else None)
        run.adapter = adapter
        run.task = asyncio.ensure_future(self._execute(run, adapter, model or self._model()))
        return run

    def start_connect(self, host: str | None = None, via: str = "web", *, model=None, status: dict | None = None) -> AgentRun:
        """Start protocol-connect-chats on this computer. `host` is the chat the owner named (`mas connect X`), or None to have the owner asked which. Nothing here needs a paired node."""
        run = AgentRun(uuid.uuid4().hex[:10], "connect", None, "this computer", via)
        self.runs[run.id] = run
        self.coord.log.add("connect_requested", {"run": run.id, "host": host, "via": via}, owner=(via != "claude-code"))
        run.model_status = status or self.model_status()
        run.store = Store(self.dir / run.id)
        run.target = A.new_target(run.store, TargetKind.chat_connect, "this-computer")
        self._started(run)
        if host:  # the chat named in the request IS the owner's answer to "which chats?": recorded as an OWNER event from the request (the operator's start is the operator's, never the owner's)
            A.write_owner_event(run.store, "owner_answer", {"question": "which_chats", "option": host, "from": "the request", **({"by": "claude-code"} if via == "claude-code" else {})}, None)
        if not run.model_status["available"] and model is None:
            run.status, run.reason = "no_model", "no model available on the coordinator: " + (run.model_status.get("reason") or "")
            run.finished = time.time()
            return run
        approve = lambda h, n, a, by: self.coord.log.add("setup_step_approved", {"run": run.id, "node": "this-computer", "plan_hash": h, "step": n, "action": a, **({"by": "claude-code"} if by != "owner" else {})}, owner=True)  # noqa: E731
        from envrec_coordinator import connect as CN
        adapter = ConnectAdapter(detect=lambda: CN.detect(self.coord.connect_env), build=lambda h: CN.build_plan(h, self.coord.connect_env), preselected=host or None,
                                 start_step=lambda plan, n, action: self.coord.connect_step(plan, n, action), approve=approve, owner_events=self._owner_event,
                                 plan_approval=approval.mechanism_for("connect") if approval.mechanism_for("connect") == approval.CHAT_CHOICE else None)
        run.adapter = adapter
        run.task = asyncio.ensure_future(self._execute(run, adapter, model or self._model()))
        return run

    def _record_setup(self, run: AgentRun) -> None:
        """GUARD: what a setup (or an undo) run left on the machine is kept whole: the plan it ran and where each step stands. A run that changed nothing leaves an earlier record as it was; once every step
        is undone there is nothing left to record."""
        ad = run.adapter
        if not isinstance(ad, SetupAdapter) or not ad.plan:
            return
        key = getattr(ad, "record_id", None) or run.node_id  # a connection is recorded under the chat it connected
        left = [n for n, st in ad.status.items() if st in ("done", "failed")]
        path = self.setups / f"{key}.json"
        if left:
            self.setups.mkdir(parents=True, exist_ok=True)
            tmp = self.setups / f"{key}.json.tmp"
            tmp.write_text(json.dumps({"run": run.id, "node": key, "recorded_at": time.time(), "option_title": ad.plan.get("option_title") or next((x["option"].get("title") for x in [self.stored_pick(run.node_id)] if x and x["option"].get("id") == ad.plan.get("option_id")), None), "plan": ad.plan, "status": {str(n): st for n, st in ad.status.items()}}, ensure_ascii=False, default=str))
            tmp.replace(path)
            self.coord.log.add("setup_recorded", {"run": run.id, "node": key, "steps_left": len(left)})
        elif any(st == "undone" for st in ad.status.values()) and path.exists():
            path.unlink()
            self.coord.log.add("setup_record_cleared", {"run": run.id, "node": key})

    def stored_setup(self, node_id: str) -> dict | None:
        """The record of what a setup left on a machine, or None when nothing is left to undo."""
        try:
            doc = json.loads((self.setups / f"{node_id}.json").read_text())
        except (OSError, ValueError):
            return None
        ok = isinstance(doc, dict) and isinstance(doc.get("plan"), dict) and isinstance(doc.get("status"), dict) and any(st in ("done", "failed") for st in doc["status"].values())
        return doc if ok else None

    def record_pick(self, run: AgentRun) -> None:
        """The owner's pick is kept whole (the option as the code built it, with its build, model and numbers), one file per machine: the setup protocol starts from it and from nothing a model said."""
        option = next((o for o in (run.result or {}).get("options", []) if o["id"] == run.picked_option), None)
        if option is None:
            return
        self.picks.mkdir(parents=True, exist_ok=True)
        tmp = self.picks / f"{run.node_id}.json.tmp"
        tmp.write_text(json.dumps({"run": run.id, "node": run.node_id, "picked_at": time.time(), "option": option}, ensure_ascii=False, default=str))
        tmp.replace(self.picks / f"{run.node_id}.json")

    def record_pick_option(self, node_id: str, option: dict, run_id: str) -> None:
        """The same stored pick `record_pick` writes, for an option the harness built by code and the owner chose in a chat question (not in an options run)."""
        self.picks.mkdir(parents=True, exist_ok=True)
        tmp = self.picks / f"{node_id}.json.tmp"
        tmp.write_text(json.dumps({"run": run_id, "node": node_id, "picked_at": time.time(), "option": option}, ensure_ascii=False, default=str))
        tmp.replace(self.picks / f"{node_id}.json")

    # ---- tools the harness installed on a computer (one file per computer, one entry per environment) ---------------------------------------------------------

    def stored_tools(self, node_id: str) -> dict[str, dict]:
        try:
            doc = json.loads((Path(self.coord.state) / "tool_installs" / f"{node_id}.json").read_text())
        except (OSError, ValueError):
            return {}
        return doc if isinstance(doc, dict) else {}

    def _write_tools(self, node_id: str, doc: dict) -> None:
        d = Path(self.coord.state) / "tool_installs"
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f"{node_id}.json.tmp"
        tmp.write_text(json.dumps(doc, default=str))
        tmp.replace(d / f"{node_id}.json")

    def save_tool(self, node_id: str, env_id: str, doc: dict) -> None:
        self._write_tools(node_id, {**self.stored_tools(node_id), env_id: doc})

    def drop_tool(self, node_id: str, env_id: str) -> None:
        self._write_tools(node_id, {k: v for k, v in self.stored_tools(node_id).items() if k != env_id})

    def stored_pick(self, node_id: str) -> dict | None:
        try:
            doc = json.loads((self.picks / f"{node_id}.json").read_text())
        except (OSError, ValueError):
            return None
        return doc if isinstance(doc, dict) and isinstance(doc.get("option"), dict) and doc["option"].get("origin") == "catalog_runtime" else None

    # ---- the draft of the whole network ----------------------------------------------------------------------------------------------------

    @property
    def drafts(self) -> DR.DraftStore:
        return DR.DraftStore(Path(self.coord.state))

    def _network(self) -> list[dict]:
        return [{"node_id": n.node_id, "name": n.name, "stored": self.stored_analysis(n.node_id)} for n in self.coord.registry.all()]

    def start_draft(self, via: str = "web", *, correction: str | None = None, model=None, status: dict | None = None, network=None) -> AgentRun:
        """Start protocol-draft for the whole network. With `correction`, the owner's words are recorded as an OWNER event and the model drafts again from them (a new round)."""
        run = AgentRun(uuid.uuid4().hex[:10], "draft", None, "this network", via)
        self.runs[run.id] = run
        self.coord.log.add("draft_requested", {"run": run.id, "via": via, "correction": bool(correction)}, owner=(via != "claude-code"))
        text = " ".join((correction or "").split())[:600] or None
        prev = self.drafts.current()
        run.model_status = status or self.model_status()
        run.store = Store(self.dir / run.id)
        run.target = A.new_target(run.store, TargetKind.network, "this-network")
        self._started(run)
        if text:
            A.write_owner_event(run.store, "owner_correction", {"text": text, **({"by": "claude-code"} if via == "claude-code" else {})}, None)
        if not run.model_status["available"] and model is None:
            run.status, run.reason = "no_model", "no model available on the coordinator: " + (run.model_status.get("reason") or "")
            run.finished = time.time()
            return run
        round_ = prev["round"] + 1 if prev and prev.get("status") == "proposed" else 1
        adapter = DraftAdapter(network=network or self._network, store=self.drafts, owner_events=self._owner_event, correction=text, round_=round_)
        run.adapter = adapter
        run.task = asyncio.ensure_future(self._execute(run, adapter, model or self._model()))
        return run

    def _store_draft(self, run: AgentRun) -> None:
        """GUARD: a draft whose protocol closed is kept as the PROPOSED draft of its round; only the owner's approval turns it into an output."""
        doc = {**run.adapter.final, "run": run.id, "stored_at": time.time()}
        self.drafts.save_current(doc)
        self.coord.log.add("draft_stored", {"run": run.id, "round": doc["round"], "machines": len(doc["machines"])})

    def compile_draft(self, run: AgentRun) -> dict:
        doc = getattr(run.adapter, "final", None) if run.adapter is not None else None
        base = {"label": "agent draft (a model proposed it; code enforced the hard limits; nothing was installed)", "agent": True}
        if not doc:
            return {**base, "draft": None, "text": ""}
        return {**base, "draft": doc, "text": DR.draft_text(doc), "approved": False}

    def approve_draft(self, run_id: str, by: str = "owner") -> dict | None:
        """The owner approved the current proposed draft: an OWNER event, the output (picks and topology; executed: false) and the profile entries from its corrections."""
        run = self.runs.get(run_id)
        cur = self.drafts.current()
        if run is None or run.kind != "draft" or run.status != "done" or not cur or cur.get("run") != run.id or cur.get("status") != "proposed":
            return None
        out = DR.approved_output(cur)
        machines = {m: {"name": d["name"]} for m, d in cur["machines"].items()}
        A.write_owner_event(run.store, "owner_approve_draft", {"round": cur["round"], **({"by": "claude-code"} if by == "claude-code" else {})}, run.loop_run_id)
        self.drafts.save_approved({"run": run.id, "approved_at": time.time(), **out})
        self.drafts.profile_add(DR.profile_entries(cur["edits"], {**machines, **{m: {"name": s["name"]} for m, s in getattr(run.adapter, "machines", {}).items()}}, cur["round"]))
        self.drafts.save_current({**cur, "status": "approved"})
        if run.result is not None:
            run.result["approved"] = True
        self.coord.log.add("draft_approved", {"run": run.id, "round": cur["round"]}, owner=(by != "claude-code"))
        return out

    def profile_view(self) -> dict:
        return self.drafts.profile()

    def profile_reset(self) -> int:
        n = self.drafts.profile_reset()
        self.coord.log.add("owner_profile_reset", {"entries": n}, owner=True)
        return n

    # ---- the stored analysis ---------------------------------------------------------------------------------------------------------------

    def _store_analysis(self, run: AgentRun) -> None:
        """GUARD: an analysis that closed (the model's finish_analysis succeeded and the exit's evidence was in) is kept, with the evidence of each fact, for the setup-options protocol."""
        facts = {f.key: f for f in run.store.current_facts(run.target.id) if f.status != FactStatus.unknown}
        rows = {k: {"value": f.value, "status": f.status.value, "probe": (f.evidence[0].probe if f.evidence else "analysis")} for k, f in facts.items()
                if k.startswith(("machine.", "analysis.")) or k.startswith("finding.")}
        doc = {"run": run.id, "node": run.node_id, "stored_at": time.time(), "facts": rows}
        self.analyses.mkdir(parents=True, exist_ok=True)
        tmp = self.analyses / f"{run.node_id}.json.tmp"
        tmp.write_text(json.dumps(doc, ensure_ascii=False, default=str))
        tmp.replace(self.analyses / f"{run.node_id}.json")
        self.coord.log.add("analysis_stored", {"run": run.id, "node": run.node_id, "facts": len(rows)})

    def stored_analysis_record(self, node_id: str) -> dict | None:
        """The stored analysis document exactly as it lies on disk, usable or not: who asks whether one exists at all reads this, never a second copy of the path."""
        try:
            doc = json.loads((self.analyses / f"{node_id}.json").read_text())
        except (OSError, ValueError):
            return None
        return doc if isinstance(doc, dict) else None

    def stored_analysis(self, node_id: str) -> dict | None:
        """The stored analysis of a node, or None when there is none (or it is not a complete one)."""
        doc = self.stored_analysis_record(node_id)
        return doc if doc is not None and self._analysis_need() <= set(doc.get("facts", {})) else None  # an analysis made under an older, narrower spec does not count

    @staticmethod
    def _analysis_need() -> set[str]:
        """What a stored analysis must hold: the facts that the analysis protocol's own exit requires (read from its spec, so the two cannot drift apart)."""
        proto = next(sk for sk in library().protocols() if sk.meta.protocol == "analyze").meta.states
        return {n["fact"] for n in proto.states[proto.start].advance[0].needs if "fact" in n}

    def start_lan(self, via: str = "web", *, model=None, status: dict | None = None) -> AgentRun:
        run = AgentRun(uuid.uuid4().hex[:10], "lan", None, "this network", via)
        self.runs[run.id] = run
        self.coord.log.add("lan_run_requested", {"run": run.id, "via": via}, owner=(via != "claude-code"))
        run.model_status = status or self.model_status()
        run.store = Store(self.dir / run.id)
        run.target = A.new_target(run.store, TargetKind.lan, "this-network")
        self._started(run)
        if not run.model_status["available"] and model is None:
            run.status, run.reason = "no_model", "no model available on the coordinator: " + (run.model_status.get("reason") or "")
            return run
        adapter = A.LanAdapter(waiting=self.coord.waiting.list, offer_for_waiting=self.coord.offer_for_waiting, owner_events=self._owner_event, nodes=self.coord.registry.all)
        run.adapter = adapter
        run.task = asyncio.ensure_future(self._execute(run, adapter, model or self._model()))
        return run

    async def _execute(self, run: AgentRun, adapter: A.BaseAdapter, model) -> None:
        run.status = "running"
        try:
            lib = library()
            loop = HarnessLoop(run.store, [adapter], model, config=RunConfig(max_steps=MAX_STEPS, max_seconds=MAX_SECONDS, protocols=ProtocolSet.from_library(lib), library=lib))
            adapter.run_id = run.loop_run_id = loop.run_id
            s = await loop.run(run.target)
            run.summary = {"stop_reason": s.stop_reason, "steps": s.steps, "model_calls": s.model_calls, "invalid_replies": s.invalid_replies, "fallbacks": s.fallbacks, "rejected_actions": s.rejected_actions,
                           "tools_run": list(s.tools_run), "elapsed_s": round(s.elapsed_s, 1), "owner_wait_s": round(s.owner_wait_s, 1)}
            reason = s.stop_reason
            proto_done = reason == "protocol_done"
            state = (s.protocol_history.get(run.target.id) or [{}])[-1].get("state") if s.protocol_history else None
            run.status = ("done" if proto_done and state not in ("no_options", "no_machines") else "no_machines" if proto_done and state == "no_machines" else "no_options" if proto_done
                          else reason if reason in ("blocked_needs_human",) else "stopped")
            run.reason = "" if run.status in ("done", "no_options", "no_machines") else f"the run ended before the protocol did: {reason}"
            if run.kind in ("install", "uninstall", "connect"):  # the protocol's own terminal state is the status: done, rolled_back, cannot_install, nothing_to_undo or nothing_to_connect
                run.status = state if proto_done and state in ("done", "rolled_back", "cannot_install", "nothing_to_undo", "nothing_to_connect") else "blocked_needs_human" if reason == "blocked_needs_human" else "stopped"
                run.reason = "" if proto_done else f"the run ended before the protocol did: {reason}"
                if state == "nothing_to_connect" and run.kind == "connect":  # the refusal's own words are the reason the owner reads (quit the chat, then ask again), not a silent end
                    refused = next((f for f in run.store.current_facts(run.target.id) if f.key == "setup.refused"), None)
                    if refused is not None and isinstance(refused.value, dict):
                        run.reason = str(refused.value.get("text", ""))[:500]
            if run.kind == "analysis" and run.status == "done":
                self._store_analysis(run)
            if run.kind == "draft" and run.status == "done":
                self._store_draft(run)
        except Exception as exc:
            run.status, run.reason = "failed", f"the run failed: {type(exc).__name__}: {str(exc)[:160]}"
        finally:
            run.finished = time.time()
            run.result = self.compile_draft(run) if run.kind == "draft" else self.compile(run)
            if run.kind in ("install", "uninstall", "connect"):
                self._record_setup(run)
            self.coord.log.add({"analysis": "analysis_finished", "options": "options_finished", "draft": "draft_finished", "install": "install_finished", "uninstall": "uninstall_finished", "connect": "connect_finished"}.get(run.kind, "lan_run_finished"), {"run": run.id, "status": run.status, "actors": self.actor_counts(run)})

    async def _code_only(self, run: AgentRun) -> None:
        """No model: the same probes, run by CODE in a fixed order, shown as a raw report with its label. Never presented as an agent analysis."""
        facts: dict = {}
        for tt in CODE_ONLY_ORDER:
            rec = self.coord.tasks.start(run.node_id, tt, {}, by="GUARD", timeout_s=T.TIMEOUTS_S.get(tt))
            await rec.done.wait()
            run.store.append_event(Event(actor=Actor.loop, type="code_only_probe", payload={"probe": tt, "status": rec.status}, run_id="code-only", tag="SCRIPT"))
            if rec.status == "done":
                facts.update(rec.result["facts"])
        run.code_only = {"label": LABEL_CODE_ONLY, "facts": facts}
        run.finished = time.time()
        run.result = {"label": LABEL_CODE_ONLY, "agent": False, "groups": self._group(facts), "findings": [], "unknowns": [], "options": []}

    # ---- views ---------------------------------------------------------------------------------------------------------------------------------

    @staticmethod
    def _group(facts: dict) -> dict:
        def take(*prefixes):
            return {k: v for k, v in facts.items() if k.startswith(prefixes)}
        return {"system": take("machine.os."), "hardware": take("machine.ram", "machine.vram", "machine.disk", "machine.gpu", "machine.model_path", "machine.largest"),
                "programs": take("machine.programs"), "ai": take("machine.runtime", "machine.ai_cli", "machine.api_key", "machine.largest"), "security": take("machine.listening", "machine.key_owner", "machine.os_family")}

    def compile(self, run: AgentRun) -> dict:
        if run.store is None or run.target is None:
            return {}
        all_ = run.store.current_facts(run.target.id)
        facts = {f.key: f for f in all_ if f.status != FactStatus.unknown}
        status_of = {k: f.status.value for k, f in facts.items()}
        groups = self._group({k: f.value for k, f in facts.items()})
        findings = [{"text": f.value["text"], "rests_on": f.value["rests_on"], "status": f.status.value} for k, f in sorted(facts.items()) if k.startswith("finding.")]
        unknowns = [{"key": f.key, "question": next((u for u in []), "")} for f in all_ if f.status == FactStatus.unknown]
        fin = facts.get("analysis.summary")
        options = []
        for i in range(1, 4):
            o = facts.get(f"option.{i}")
            if o:
                ex = facts.get(f"option.{i}.explanation")
                options.append({**o.value, "explanation": ex.value if ex else "", "explanation_status": ex.status.value if ex else "none"})
        return {"label": S.LABEL_AGENT_ANALYSIS, "agent": True, "groups": groups, "evidence_state": status_of, "findings": findings, "unknowns": unknowns,
                "finished": fin.value if fin else None, "options": options, "option_set": facts["option.set"].value if "option.set" in facts else None, "answers": {k.removeprefix(A.ANSWER_PREFIX): f.value for k, f in facts.items() if k.startswith(A.ANSWER_PREFIX)}}

    def actor_counts(self, run: AgentRun) -> dict:
        c = Counter(e.effective_tag for e in run.store.events()) if run.store is not None else Counter({"CLAUDE-CODE": 1} if run.via == "claude-code" else {})  # a run that never got a store still says who asked
        return {k: c.get(k, 0) for k in ("MODEL", "GUARD", "SCRIPT", "OWNER", "CLAUDE-CODE")}

    def view(self, run_id: str, *, events: bool = True) -> dict | None:
        run = self.runs.get(run_id)
        if run is None:
            return None
        pending = run.adapter.pending.view() if run.adapter is not None and run.adapter.pending is not None else None
        plan = run.adapter.plan_view() if isinstance(run.adapter, SetupAdapter) else None
        out = {"id": run.id, "kind": run.kind, "plan": plan, "node": run.node_name, "node_id": run.node_id, "status": run.status, "reason": run.reason, "started": run.started, "finished": run.finished, "via": run.via,
               "summary": run.summary, "pending": pending, "model": run.model_status, "picked_option": run.picked_option, "actors": self.actor_counts(run), "result": run.result if run.finished else None}
        if run.kind == "draft":
            out["intents"] = self.draft_intents(run, pending)
        if events and run.store is not None:
            rows = [e for e in run.store.events() if e.type in SHOWN]
            out["events"] = [{"n": i, "tag": e.effective_tag, "type": e.type, "text": describe_event(e)} for i, e in enumerate(rows[-80:], 1)]
        return out

    def draft_intents(self, run: AgentRun, pending: dict | None) -> list[dict]:
        """Intents (not screens) for the Harness App: the draft as `show_data` once it exists, the open question as `ask_choice`; each carries its text fallback in English."""
        out: list[dict] = []
        doc = (run.result or {}).get("draft") if run.result else None
        if doc:
            out.append(DR.show_data_intent(doc, run.id))
        if pending:
            out.append(DR.ask_choice_intent(pending, run.id))
        return out

    def listing(self) -> list[dict]:
        return [{"id": r.id, "kind": r.kind, "node": r.node_name, "node_id": r.node_id, "status": r.status, "pending": bool(r.adapter and r.adapter.pending), "finished": r.finished} for r in sorted(self.runs.values(), key=lambda r: r.started)[-10:]]

    # ---- the owner ---------------------------------------------------------------------------------------------------------------------------

    def answer(self, run_id: str, qid: str, option: str, by: str = "owner", channel: str | None = None, gesture: str | None = None) -> bool:
        run = self.runs.get(run_id)
        if run and run.adapter and by == "owner" and self.needs_gesture(run, qid) and gesture not in OWNER_GESTURES:
            # The approval of a step that WRITES counts as the owner's only when it comes with the human gesture that made it (the panel, the system dialog, the chat's own dialog). An answer
            # that merely says "owner" (a chat adapter's tool path, a direct call) is refused, and the decision stays open.
            self.coord.log.add("owner_authority_refused", {"run": run_id, "question": qid, "channel": channel, "gesture": gesture})
            return False
        return bool(run and run.adapter and run.adapter.answer(qid, option, by, channel))

    def cancel(self, run_id: str, by: str = "owner") -> tuple[bool, str]:
        """End a run that has not finished (it may be waiting for the owner on a question nobody will answer). (True, status) or (False, why). Nothing it already did is undone: that is `mas_undo`."""
        run = self.runs.get(run_id)
        if run is None:
            return False, "unknown_run"
        if run.finished is not None:
            return False, f"already_{run.status}"
        if run.adapter is not None and run.adapter.pending is not None and not run.adapter.pending.future.done():
            run.adapter.pending.future.cancel()
        if run.task is not None and not run.task.done():
            run.task.cancel()
        run.status, run.reason, run.finished = "cancelled", f"cancelled by the {by}", time.time()
        self.coord.log.add("run_cancelled", {"run": run_id, "kind": run.kind, "by": by}, owner=(by == "owner"))
        return True, "cancelled"

    @staticmethod
    def needs_gesture(run: AgentRun, qid: str) -> bool:
        p = run.adapter.pending if run.adapter is not None else None
        return qid.startswith("approve_") or bool(p is not None and p.qid == qid and p.data.get("kind") == "approve_step")

    def pick_option(self, run_id: str, option_id: str, channel: str | None = None, by: str | None = None) -> bool:
        """The owner picked one of the options. Recorded as an OWNER event; nothing is installed in this step."""
        run = self.runs.get(run_id)
        if run is None or run.result is None or option_id not in {o["id"] for o in run.result.get("options", [])}:
            return False
        run.picked_option = option_id
        self.record_pick(run)
        A.write_owner_event(run.store, "owner_pick_option", {"option": option_id, **({"channel": channel} if channel else {}), **({"by": "claude-code"} if by == "claude-code" else {})}, run.loop_run_id)
        self.coord.log.add("setup_option_picked", {"run": run.id, "node": run.node_id, "option": option_id, **({"channel": channel} if channel else {})}, owner=(by != "claude-code"))
        return True

    async def shutdown(self) -> None:
        """The coordinator is stopping: end every run (a pending question is simply never answered) and close its log."""
        for run in self.runs.values():
            if run.task is not None and not run.task.done():
                run.task.cancel()
        await asyncio.gather(*[r.task for r in self.runs.values() if r.task is not None], return_exceptions=True)
        for run in self.runs.values():
            if run.store is not None:
                try:
                    run.store.close()
                except Exception:
                    pass

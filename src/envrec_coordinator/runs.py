"""Harness runs on the coordinator: the agent loop for a machine analysis (protocol-analyze) and for pairing offers (protocol-lan).

`AgentRuns.start_analysis` is what the owner's "Analizar esta máquina" does: it starts a run in which the MODEL picks one read-only probe per turn from a closed menu. If no model is
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
from envrec_net import task as T

MAX_STEPS = 40
MAX_SECONDS = 900.0
CODE_ONLY_ORDER = ("os_details", "memory_for_models", "gpus", "model_runtimes", "ai_clis", "installed_programs")
LABEL_CODE_ONLY = "code-only, no model: these are the raw probe results; no agent interpreted them"


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
        self.status = "starting"  # starting | running | done | no_options | blocked_needs_human | stopped | no_model | failed
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
    p = e.payload
    t = e.type
    if t == "action_chosen":
        return f"chose {p.get('action')}" + (f" {p.get('tool')}" if p.get("tool") else "") + (f": {_short(p.get('why'))}" if p.get("why") else "")
    if t == "tool_run":
        return f"ran {p.get('tool')} on the machine"
    if t == "tool_result":
        return f"{p.get('tool')}: " + ("ok" if p.get("ok", True) else f"failed ({_short(p.get('error'))})") + (f", {p.get('facts')} facts" if p.get("facts") is not None else "")
    if t == "protocol_state":
        return f"protocol {p.get('from')} → {p.get('to')} (by {p.get('by')})"
    if t == "run_requested":
        return "asked for this run"
    if t == "owner_answer":
        return f"answered {p.get('question')}: {p.get('option')}" + (" (the operator, for the owner)" if p.get("by") == "claude-code" else "")
    if t == "owner_pick":
        return f"picked waiting computer {p.get('waiting')}"
    if t == "owner_pick_option":
        return f"picked option {p.get('option')}"
    if t == "run_end":
        return f"run ended: {p.get('stop_reason')}"
    if t in ("action_rejected", "protocol_refused", "policy_decision"):
        return f"{t.replace('_', ' ')}: {_short(p.get('reason') or p.get('decision') or p.get('action'))}"
    if t == "code_only_probe":
        return f"code ran {p.get('probe')} ({p.get('status')})"
    return t.replace("_", " ")


SHOWN = {"run_requested", "action_chosen", "tool_run", "tool_result", "protocol_state", "owner_answer", "owner_pick", "owner_pick_option", "run_end", "action_rejected", "protocol_refused", "code_only_probe", "run_start"}


class AgentRuns:
    def __init__(self, coord, *, model_profile: str | None = None, models_file: Path | None = None) -> None:
        self.coord, self.model_profile = coord, model_profile
        self.models_file = models_file or Path("config/models.yaml")
        self.runs: dict[str, AgentRun] = {}
        self.dir = Path(coord.state) / "runs"
        self.analyses = Path(coord.state) / "analyses"  # the finished analysis of each machine, one file per node: what the setup-options protocol reads

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
        A.write_owner_event(run.store, "run_requested", {"kind": run.kind, "via": run.via, **({"by": "claude-code"} if run.via == "claude-code" else {})}, None)  # OWNER for the owner's press, CLAUDE-CODE for the operator's

    def start_analysis(self, node_id: str, via: str = "web", *, model=None, status: dict | None = None) -> AgentRun:
        node = self.coord.registry.get(node_id)
        run = AgentRun(uuid.uuid4().hex[:10], "analysis", node_id, node.name if node else node_id, via)
        self.runs[run.id] = run
        self.coord.log.add("analysis_requested", {"run": run.id, "node": node_id, "via": via}, owner=(via != "claude-code"))  # the OWNER pressed the button, unless the start says it was the operator (CLAUDE-CODE)
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
        adapter = A.OptionsAdapter(node_id=node_id, node_name=run.node_name, load_stored=lambda: self.stored_analysis(node_id), owner_events=self._owner_event)
        run.adapter = adapter
        run.task = asyncio.ensure_future(self._execute(run, adapter, model or self._model()))
        return run

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

    def stored_analysis(self, node_id: str) -> dict | None:
        """The stored analysis of a node, or None when there is none (or it is not a complete one)."""
        try:
            doc = json.loads((self.analyses / f"{node_id}.json").read_text())
        except (OSError, ValueError):
            return None
        return doc if isinstance(doc, dict) and self._analysis_need() <= set(doc.get("facts", {})) else None  # an analysis made under an older, narrower spec does not count

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
            run.status = "done" if proto_done and state != "no_options" else "no_options" if proto_done else reason if reason in ("blocked_needs_human",) else "stopped"
            run.reason = "" if run.status in ("done", "no_options") else f"the run ended before the protocol did: {reason}"
            if run.kind == "analysis" and run.status == "done":
                self._store_analysis(run)
        except Exception as exc:
            run.status, run.reason = "failed", f"the run failed: {type(exc).__name__}: {str(exc)[:160]}"
        finally:
            run.finished = time.time()
            run.result = self.compile(run)
            self.coord.log.add({"analysis": "analysis_finished", "options": "options_finished"}.get(run.kind, "lan_run_finished"), {"run": run.id, "status": run.status, "actors": self.actor_counts(run)})

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
        return {"label": "agent analysis (a model chose the probes; code ran them)", "agent": True, "groups": groups, "evidence_state": status_of, "findings": findings, "unknowns": unknowns,
                "finished": fin.value if fin else None, "options": options, "option_set": facts["option.set"].value if "option.set" in facts else None, "answers": {k.removeprefix(A.ANSWER_PREFIX): f.value for k, f in facts.items() if k.startswith(A.ANSWER_PREFIX)}}

    def actor_counts(self, run: AgentRun) -> dict:
        c = Counter(e.effective_tag for e in run.store.events()) if run.store is not None else Counter({"CLAUDE-CODE": 1} if run.via == "claude-code" else {})  # a run that never got a store still says who asked
        return {k: c.get(k, 0) for k in ("MODEL", "GUARD", "SCRIPT", "OWNER", "CLAUDE-CODE")}

    def view(self, run_id: str, *, events: bool = True) -> dict | None:
        run = self.runs.get(run_id)
        if run is None:
            return None
        pending = run.adapter.pending.view() if run.adapter is not None and run.adapter.pending is not None else None
        out = {"id": run.id, "kind": run.kind, "node": run.node_name, "node_id": run.node_id, "status": run.status, "reason": run.reason, "started": run.started, "finished": run.finished, "via": run.via,
               "summary": run.summary, "pending": pending, "model": run.model_status, "picked_option": run.picked_option, "actors": self.actor_counts(run), "result": run.result if run.finished else None}
        if events and run.store is not None:
            rows = [e for e in run.store.events() if e.type in SHOWN]
            out["events"] = [{"n": i, "tag": e.effective_tag, "type": e.type, "text": describe_event(e)} for i, e in enumerate(rows[-80:], 1)]
        return out

    def listing(self) -> list[dict]:
        return [{"id": r.id, "kind": r.kind, "node": r.node_name, "node_id": r.node_id, "status": r.status, "pending": bool(r.adapter and r.adapter.pending), "finished": r.finished} for r in sorted(self.runs.values(), key=lambda r: r.started)[-10:]]

    # ---- the owner ---------------------------------------------------------------------------------------------------------------------------

    def answer(self, run_id: str, qid: str, option: str, by: str = "owner") -> bool:
        run = self.runs.get(run_id)
        return bool(run and run.adapter and run.adapter.answer(qid, option, by))

    def pick_option(self, run_id: str, option_id: str) -> bool:
        """The owner picked one of the options. Recorded as an OWNER event; nothing is installed in this step."""
        run = self.runs.get(run_id)
        if run is None or run.result is None or option_id not in {o["id"] for o in run.result.get("options", [])}:
            return False
        run.picked_option = option_id
        A.write_owner_event(run.store, "owner_pick_option", {"option": option_id}, run.loop_run_id)
        self.coord.log.add("setup_option_picked", {"run": run.id, "node": run.node_id, "option": option_id}, owner=True)
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

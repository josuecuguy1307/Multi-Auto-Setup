"""The harness agent on the coordinator (protocol-analyze and protocol-lan).

A MODEL picks one action per turn from a closed menu; CODE validates it, runs it, records it. The tools below are typed and read-only: the node probes run on the paired machine through the
signed task mechanism (`tasks.py`), the owner's questions are closed (ids and options come from the skill's `questions.json`, never from the model), and the options are built by code
(`options.py`). Every fact carries its evidence; a model's interpretation is a `hypothesis` with the facts it rests on. The OWNER's answers and picks are events tagged OWNER.

Nothing here installs or writes on a node. `pair_offer_start` refuses unless the OWNER picked that waiting machine in this run."""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

import envrec_os
from envrec.adapters import Adapter, ToolContext
from envrec.contracts import Actor, CreatedBy, Event, Evidence, Fact, FactStatus, ProbeResult, Target, TargetKind, Tier, ToolSpec, Unknown
from envrec.contracts import Coverage
from envrec_coordinator import options as O
from envrec_net import task as T

QUESTIONS_FILE = envrec_os.SKILLS_DIR / "protocol-setup-options" / "references" / "questions.json"
LAN_QUESTIONS_FILE = envrec_os.SKILLS_DIR / "protocol-lan" / "references" / "questions.json"
OWNER_WAIT_S = 900.0
ANSWER_PREFIX = "owner.answer."

# probe tool -> (task type, the facts it writes come from the node's typed result)
PROBE_TOOLS = {
    "node_os_details": ("os_details", "the operating system, version and real build of the machine"),
    "node_programs": ("installed_programs", "the programs installed on the machine"),
    "node_gpu": ("gpus", "the graphics cards of the machine and their memory"),
    "node_runtimes": ("model_runtimes", "local model runtimes installed or running"),
    "node_ai_clis": ("ai_clis", "official AI command-line tools installed"),
    "node_memory_fit": ("memory_for_models", "memory for models and the largest model that fits"),
}

# the fact keys each probe's node result carries (a test compares these with what the probes really return): the view uses them to name the tool that produces a key an exit lacks
PROBE_PRODUCES = {
    "node_os_details": ["machine.os.name", "machine.os.version", "machine.os.build", "machine.os.arch"],
    "node_programs": ["machine.programs_count", "machine.programs"],
    "node_gpu": ["machine.gpu_count", "machine.gpus"],
    "node_runtimes": ["machine.runtime_count", "machine.runtimes"],
    "node_ai_clis": ["machine.ai_cli_count", "machine.ai_clis", "machine.api_key_variable_names"],
    "node_memory_fit": ["machine.ram_total_gib", "machine.ram_available_gib", "machine.vram_gib", "machine.model_path", "machine.disk_free_gib", "machine.largest_model_params_b",
                        "machine.largest_model_limited_by"],
}


def questions(path: Path = QUESTIONS_FILE) -> dict:
    return json.loads(path.read_text())


def _schema(**props) -> dict:
    """A tool's argument form, as the menu shows it to the model. Every argument given here is required: a tool that takes a required argument can be asked again with a different one."""
    return {"type": "object", "properties": {k: {"type": "string", "description": v} for k, v in props.items()}, "required": list(props)}


def _spec(name: str, line: str, *, kinds: list[TargetKind], produces: list[str] | None = None, schema: dict | None = None, tier: Tier = Tier.observe, requires: list[str] | None = None,
          repeatable: bool = False) -> ToolSpec:
    return ToolSpec(name=name, ring=0, tier=tier, target_kinds=kinds, one_line=line, source="coordinator", produces=produces or [], requires=requires or [], input_model=schema or {}, repeatable=repeatable)


def _fact(target_id: str, key: str, value: Any, tool: str, provider: str, *, status: FactStatus = FactStatus.tested, created_by: CreatedBy = CreatedBy.probe, event_id: str | None = None,
          confidence: float = 0.9, limitations: list[str] | None = None) -> Fact:
    return Fact(target_id=target_id, key=key, value=value, status=status, created_by=created_by,
                evidence=[Evidence(provider=provider, probe=tool, event_id=event_id, confidence=confidence, limitations=(limitations or [])[:6])])


@dataclass
class Pending:
    qid: str
    text: str
    options: list[dict]
    future: asyncio.Future = field(default_factory=lambda: asyncio.get_running_loop().create_future())
    asked_at: float = field(default_factory=time.time)
    answered_by: str = "owner"  # "owner", or "claude-code" when the operator answered for the owner (authorized in chat): recorded, and never counted as the owner

    def view(self) -> dict:
        return {"question": self.qid, "text": self.text, "options": self.options, "asked_at": self.asked_at}


class BaseAdapter(Adapter):
    name = "coordinator"

    def __init__(self, *, owner_events) -> None:
        self.owner_wait_s = 0.0  # seconds spent waiting for the owner: the loop does not count it against its time cap
        self.pending: Pending | None = None
        self.run_id: str | None = None
        self._owner_event = owner_events  # (store, type, payload) -> writes an event tagged OWNER

    def _owner(self, store, type: str, payload: dict) -> None:
        self._owner_event(store, type, payload, self.run_id)

    async def _ask(self, ctx: ToolContext, qid: str, text: str, options: list[dict]) -> str | None:
        """Put one closed question to the OWNER and wait for the answer (the owner's time is not the run's time). None: nobody answered in time."""
        self.pending = Pending(qid, text, options)
        self.last_answered_by = "owner"
        pend = self.pending
        t0 = time.monotonic()
        try:
            answer = await asyncio.wait_for(pend.future, OWNER_WAIT_S)
            self.last_answered_by = pend.answered_by
            return answer
        except TimeoutError:
            return None
        finally:
            self.owner_wait_s += time.monotonic() - t0
            self.pending = None

    def answer(self, qid: str, option: str, by: str = "owner") -> bool:
        p = self.pending
        if p is None or p.qid != qid or option not in {o["id"] for o in p.options} or p.future.done():
            return False
        p.answered_by = by
        p.future.set_result(option)
        return True


class AnalysisAdapter(BaseAdapter):
    """Tools of protocol-analyze: the node probes, the model's findings, and `finish_analysis`, whose success ends the analysis (the guard closes the protocol and the coordinator stores it)."""

    def __init__(self, *, node_id: str, node_name: str, run_task, owner_events) -> None:
        super().__init__(owner_events=owner_events)
        self.node_id, self.node_name, self.run_task = node_id, node_name, run_task  # run_task(task_type, params) -> TaskRecord (awaitable done)
        self.findings = 0

    def tool_specs(self) -> list[ToolSpec]:
        k = [TargetKind.coordinator]
        by = {name: _spec(name, f"Read {desc}.", kinds=k, produces=PROBE_PRODUCES[name]) for name, (_t, desc) in PROBE_TOOLS.items()}
        by.update({
            "node_security_report": _spec("node_security_report", "Read the node's listening ports and key permissions.", kinds=k),
            "record_finding": _spec("record_finding", "Record your reading of the facts.", kinds=k, schema=_schema(text="what you conclude, one sentence", facts="the fact keys it rests on, comma separated")),
            "finish_analysis": _spec("finish_analysis", "State what is known, unknown and why; this ends the analysis.", kinds=k, produces=["analysis.finished", "analysis.summary"],
                                     schema=_schema(known="what is known", unknown="what is still unknown", why="why it is unknown or enough")),
        })
        # The menu is capped (a small model sees at most `menu_max` tools per turn) and keeps this order; the loop moves the producers of a missing key to the front.
        order = ["node_os_details", "node_memory_fit", "record_finding", "finish_analysis", "node_gpu", "node_runtimes", "node_ai_clis", "node_programs", "node_security_report"]
        return [by[n] for n in order]

    # ---- run -----------------------------------------------------------------------------------------------------------------------------------

    async def run(self, tool: str, args: dict[str, Any], ctx: ToolContext) -> ProbeResult:
        try:
            if tool in PROBE_TOOLS:
                return await self._probe(PROBE_TOOLS[tool][0], tool, ctx)
            if tool == "node_security_report":
                return await self._security(ctx)
            handler = {"record_finding": self._finding, "finish_analysis": self._finish}.get(tool)
            if handler is None:
                return ProbeResult(ok=False, error=f"unknown tool {tool}")
            return await handler(args, ctx)
        except Exception as exc:  # a tool never takes the run down
            return ProbeResult(ok=False, error=f"{type(exc).__name__}: {str(exc)[:200]}")

    async def _probe(self, task_type: str, tool: str, ctx: ToolContext) -> ProbeResult:
        rec = self.run_task(task_type, {})
        await rec.done.wait()
        if rec.status != "done":
            why = T.REJECTIONS.get(rec.reason, rec.reason) or rec.status
            return ProbeResult(ok=False, error=f"the node did not return it: {why}"[:300])
        facts = [_fact(ctx.target.id, key, value, tool, f"node:{self.node_name}", event_id=ctx.event_id, limitations=rec.result.get("limits", [])) for key, value in rec.result["facts"].items()]
        limits = rec.result.get("limits", [])
        unknowns = [Unknown(key=f"machine.{tool}", question=x[:200]) for x in limits[:3]]
        return ProbeResult(ok=True, facts=facts, unknowns=unknowns, coverage=Coverage(scope=tool, status="partial" if limits else "complete", checked=[f.key for f in facts], unknown=[u.key for u in unknowns]))

    async def _security(self, ctx: ToolContext) -> ProbeResult:
        rec = self.run_task("node_report", {})
        await rec.done.wait()
        if rec.status != "done":
            return ProbeResult(ok=False, error=f"the node did not return it: {T.REJECTIONS.get(rec.reason, rec.reason) or rec.status}"[:300])
        r = rec.result
        vals = {"machine.listening_ports_count": len(r.get("sockets", [])), "machine.key_owner_only": (r.get("key") or {}).get("owner_only"), "machine.os_family": (r.get("os") or {}).get("family")}
        return ProbeResult(ok=True, facts=[_fact(ctx.target.id, k, v, "node_security_report", f"node:{self.node_name}", event_id=ctx.event_id) for k, v in vals.items() if v is not None])

    async def _finding(self, args: dict, ctx: ToolContext) -> ProbeResult:
        text = " ".join(str(args.get("text", "")).split())[:300]
        raw = args.get("facts") or []
        keys = [k.strip() for k in (raw.split(",") if isinstance(raw, str) else raw) if isinstance(k, str) and k.strip()][:8]
        known = {f.key for f in ctx.store.current_facts(ctx.target.id) if f.status != FactStatus.unknown}
        if not text or not keys:
            return ProbeResult(ok=False, error="a finding needs text and the fact keys it rests on")
        missing = [k for k in keys if k not in known]
        if missing:
            return ProbeResult(ok=False, error=f"these facts do not exist yet: {missing[:4]}; a finding rests only on facts that were read")
        self.findings += 1
        f = _fact(ctx.target.id, f"finding.n{self.findings}", {"text": text, "rests_on": keys}, "record_finding", "model", status=FactStatus.hypothesis, created_by=CreatedBy.model,
                  event_id=ctx.event_id, confidence=0.5)
        return ProbeResult(ok=True, facts=[f])

    async def _finish(self, args: dict, ctx: ToolContext) -> ProbeResult:
        """The analysis is offered for finishing only when the protocol's exit evidence is in (the loop gates the tool from the spec), so nothing about WHICH facts is decided here."""
        v = {k: " ".join(str(args.get(k, "")).split())[:400] for k in ("known", "unknown", "why")}
        if not v["known"]:
            return ProbeResult(ok=False, error="say what is known")
        done = _fact(ctx.target.id, "analysis.finished", True, "finish_analysis", "envrec_coordinator.agent", created_by=CreatedBy.loop, event_id=ctx.event_id, confidence=1.0,
                     limitations=["the model declared the analysis finished and the guard accepted it; what it says is known is analysis.summary, the model's own reading"])
        summary = _fact(ctx.target.id, "analysis.summary", v, "finish_analysis", "model", status=FactStatus.hypothesis, created_by=CreatedBy.model, event_id=ctx.event_id, confidence=0.5)
        return ProbeResult(ok=True, facts=[done, summary])


class OptionsAdapter(BaseAdapter):
    """Tools of protocol-setup-options: read the STORED analysis (no re-probing), ask the owner closed questions, look up the catalog, build the options (code applies the filters), explain each."""

    def __init__(self, *, node_id: str, node_name: str, load_stored, owner_events, catalog: dict | None = None) -> None:
        super().__init__(owner_events=owner_events)
        self.node_id, self.node_name, self.load_stored = node_id, node_name, load_stored  # load_stored() -> the stored analysis of this node, or None
        self.catalog = catalog
        self.options_built: list[dict] = []
        self.explained: set[str] = set()

    def tool_specs(self) -> list[ToolSpec]:
        k = [TargetKind.setup]
        by = {
            "load_analysis": _spec("load_analysis", "Read the stored analysis of this machine (nothing is probed again).", kinds=k, produces=["analysis.loaded"]),
            "ask_owner": _spec("ask_owner", "Ask the owner one closed question.", kinds=k, schema=_schema(question="one of: " + ", ".join(sorted(questions())))),
            "catalog_lookup": _spec("catalog_lookup", "Look up what the catalog offers.", kinds=k),
            "evidence_lookup": _spec("evidence_lookup", "Look up public evidence on a topic.", kinds=k, schema=_schema(topic="a runtime or model name")),
            "build_options": _spec("build_options", "Build the setup options from the stored analysis and the owner's answers.", kinds=k, repeatable=True, requires=["analysis.loaded"], produces=["option.set", "option.none"]),
            "explain_option": _spec("explain_option", "Explain one option in plain words.", kinds=k, produces=["option.explained"], requires=["option.set"], repeatable=True,
                                    schema=_schema(option=self._option_hint(), text="why it is there and what limits it")),
        }
        order = ["load_analysis", "ask_owner", "build_options", "explain_option", "catalog_lookup", "evidence_lookup"]
        return [by[n] for n in order]

    def _option_hint(self) -> str:
        """The argument of explain_option, as the model must be able to fill it: the ids of the options the code built that are not explained yet (the facts option.N sit behind the view's budget)."""
        left = [o["id"] for o in self.options_built if o["id"] not in self.explained]
        return ("one of: " + ", ".join(left) + " (the options not explained yet)") if left else "an option id from the facts option.N"

    async def run(self, tool: str, args: dict[str, Any], ctx: ToolContext) -> ProbeResult:
        try:
            handler = {"load_analysis": self._load, "ask_owner": self._ask_owner, "catalog_lookup": self._catalog, "evidence_lookup": self._evidence, "build_options": self._build,
                       "explain_option": self._explain}.get(tool)
            if handler is None:
                return ProbeResult(ok=False, error=f"unknown tool {tool}")
            return await handler(args, ctx)
        except Exception as exc:
            return ProbeResult(ok=False, error=f"{type(exc).__name__}: {str(exc)[:200]}")

    async def _load(self, args: dict, ctx: ToolContext) -> ProbeResult:
        stored = self.load_stored()
        if not stored:
            return ProbeResult(ok=False, error="no stored analysis for this machine: analyse it first")
        src = f"stored analysis {stored['run']}"
        facts = [Fact(target_id=ctx.target.id, key=k, value=row["value"], status=FactStatus(row["status"]), created_by=CreatedBy.probe,
                      evidence=[Evidence(provider=src, probe=row.get("probe", "analysis"), event_id=ctx.event_id, confidence=0.9, limitations=["read from the stored analysis, not probed again"])])
                 for k, row in sorted(stored["facts"].items())]
        facts.append(_fact(ctx.target.id, "analysis.loaded", {"run": stored["run"], "stored_at": stored["stored_at"], "facts": len(facts)}, "load_analysis", src, event_id=ctx.event_id, confidence=1.0))
        return ProbeResult(ok=True, facts=facts)

    async def _ask_owner(self, args: dict, ctx: ToolContext) -> ProbeResult:
        qid = str(args.get("question", ""))
        q = questions().get(qid)
        if q is None:
            return ProbeResult(ok=False, error=f"unknown question {qid!r}; choose one of {sorted(questions())}")
        facts = {f.key: f.value for f in ctx.store.current_facts(ctx.target.id) if f.status != FactStatus.unknown}
        if q.get("dynamic") == "ai_clis":
            options = [{"id": c["id"], "label": c["name"], "detail": "installed" + ("" if c.get("set_up") else ", no sign-in found")} for c in facts.get("machine.ai_clis", [])]
        elif q.get("dynamic") == "providers":
            options = [{"id": p["id"], "label": p["name"], "detail": ""} for p in O.load_harnesses()["api_providers"]]
        else:
            options = q["options"]
        if not options:
            return ProbeResult(ok=False, error=f"there is nothing to choose from for {qid} (the probes found no candidates)")
        if self.pending is not None:
            return ProbeResult(ok=False, error="a question is already waiting for the owner")
        answer = await self._ask(ctx, qid, q["text"], options)
        if answer is None:
            return ProbeResult(ok=False, error="the owner did not answer in time")
        by = self.last_answered_by
        self._owner(ctx.store, "owner_answer", {"question": qid, "option": answer, **({"by": by} if by != "owner" else {})})
        f = Fact(target_id=ctx.target.id, key=f"{ANSWER_PREFIX}{qid}", value=answer, status=FactStatus.tested, created_by=CreatedBy.human,
                 evidence=[Evidence(provider="human" if by == "owner" else "operator (claude-code) for the owner", probe="ask_owner", event_id=ctx.event_id, confidence=1.0,
                                    limitations=[] if by == "owner" else ["the operator answered on the owner's behalf in a simulated-local verification; the owner authorized it in chat and did not answer himself"])])
        return ProbeResult(ok=True, facts=[f])

    async def _catalog(self, args: dict, ctx: ToolContext) -> ProbeResult:
        s = O.catalog_summary(self.catalog if self.catalog is not None else O.load_catalog())
        return ProbeResult(ok=True, facts=[_fact(ctx.target.id, "option.catalog", s, "catalog_lookup", "catalog.lock.yaml", confidence=1.0)])

    async def _evidence(self, args: dict, ctx: ToolContext) -> ProbeResult:
        topic = " ".join(str(args.get("topic", "")).split())[:60]
        if not topic:
            return ProbeResult(ok=False, error="name a topic")
        e = O.evidence_for(O.load_evidence(), topic)
        return ProbeResult(ok=True, facts=[_fact(ctx.target.id, "option.evidence", {"topic": topic, **e}, "evidence_lookup", "evidence.json", confidence=1.0)])

    def _facts_of(self, ctx: ToolContext) -> dict:
        return {f.key: f.value for f in ctx.store.current_facts(ctx.target.id) if f.status != FactStatus.unknown}

    async def _build(self, args: dict, ctx: ToolContext) -> ProbeResult:
        facts = self._facts_of(ctx)
        choice = facts.get(f"{ANSWER_PREFIX}which_ai")
        if choice is None:
            return ProbeResult(ok=False, error="ask the owner which_ai first: the options are built from the owner's choice")
        res = O.build_options(facts, choice=str(choice), sensitivity=str(facts.get(f"{ANSWER_PREFIX}sensitivity", "internal")), chosen_cli=facts.get(f"{ANSWER_PREFIX}which_cli"),
                              chosen_provider=facts.get(f"{ANSWER_PREFIX}which_api_provider"), catalog=self.catalog)
        self.options_built = res["options"]
        out = [_fact(ctx.target.id, "option.set", {"choice": res["choice"], "ids": [o["id"] for o in res["options"]], "considered": res["considered"], "kept": res["kept"], "rule": res["rule"]},
                     "build_options", "envrec_coordinator.options", event_id=ctx.event_id, confidence=1.0)]
        out += [_fact(ctx.target.id, f"option.{i}", o, "build_options", "envrec_coordinator.options", event_id=ctx.event_id, confidence=1.0) for i, o in enumerate(res["options"], 1)]
        if not res["options"]:
            out.append(_fact(ctx.target.id, "option.none", {"removed": res["removed"][:10]}, "build_options", "envrec_coordinator.options", event_id=ctx.event_id, confidence=1.0))
        return ProbeResult(ok=True, facts=out)

    async def _explain(self, args: dict, ctx: ToolContext) -> ProbeResult:
        oid, text = str(args.get("option", "")), " ".join(str(args.get("text", "")).split())[:400]
        ids = [o["id"] for o in self.options_built]
        if oid not in ids:
            return ProbeResult(ok=False, error=f"no such option; the options are {ids}")
        if not text:
            return ProbeResult(ok=False, error="say in plain words why this option is there and what limits it")
        self.explained.add(oid)
        out = [_fact(ctx.target.id, f"option.{ids.index(oid) + 1}.explanation", text, "explain_option", "model", status=FactStatus.hypothesis, created_by=CreatedBy.model, event_id=ctx.event_id, confidence=0.5)]
        if self.explained >= set(ids):
            out.append(_fact(ctx.target.id, "option.explained", True, "explain_option", "envrec_coordinator.agent", created_by=CreatedBy.loop, event_id=ctx.event_id, confidence=1.0))
        return ProbeResult(ok=True, facts=out)


class LanAdapter(BaseAdapter):
    """Tools of protocol-lan: the waiting machines, the owner's pick, and the pairing offer that the pick allows."""

    def __init__(self, *, waiting, offer_for_waiting, owner_events, nodes) -> None:
        super().__init__(owner_events=owner_events)
        self.waiting, self.offer_for_waiting, self.nodes = waiting, offer_for_waiting, nodes
        self.picks: set[str] = set()  # waiting ids the OWNER picked in THIS run

    def tool_specs(self) -> list[ToolSpec]:
        k = [TargetKind.lan]
        return [_spec("lan_discover_nodes", "List the computers waiting to be paired.", kinds=k, repeatable=True), _spec("nodes_list", "List the paired computers.", kinds=k, repeatable=True),
                _spec("ask_owner", "Ask the owner which waiting computer to pair.", kinds=k, schema=_schema(question="pick_waiting_machine")),
                _spec("pair_offer_start", "Make the pairing code for the picked computer.", kinds=k, schema=_schema(machine="the waiting id the owner picked"))]

    async def run(self, tool: str, args: dict[str, Any], ctx: ToolContext) -> ProbeResult:
        try:
            if tool == "lan_discover_nodes":
                rows = [w.view() for w in self.waiting()]
                f = [_fact(ctx.target.id, "lan.waiting_count", len(rows), tool, "coordinator.waiting", event_id=ctx.event_id)]
                f += [_fact(ctx.target.id, f"lan.waiting.{w['id'].replace('-', '_')}", {k: w[k] for k in ("id", "name", "os", "arch", "fingerprint")}, tool, "coordinator.waiting", event_id=ctx.event_id,
                            limitations=["name, system and fingerprint are claimed by the computer; the pairing exchange is what authenticates it"]) for w in rows]
                return ProbeResult(ok=True, facts=f)
            if tool == "nodes_list":
                n = self.nodes()
                return ProbeResult(ok=True, facts=[_fact(ctx.target.id, "lan.paired_count", len(n), tool, "coordinator.registry", event_id=ctx.event_id)])
            if tool == "ask_owner":
                return await self._pick(ctx)
            if tool == "pair_offer_start":
                return await self._offer(args, ctx)
            return ProbeResult(ok=False, error=f"unknown tool {tool}")
        except Exception as exc:
            return ProbeResult(ok=False, error=f"{type(exc).__name__}: {str(exc)[:200]}")

    async def _pick(self, ctx: ToolContext) -> ProbeResult:
        rows = self.waiting()
        if not rows:
            return ProbeResult(ok=False, error="no computer is waiting: there is nothing for the owner to pick")
        options = [{"id": w.id, "label": w.name, "detail": f"{w.os}/{w.arch} · {w.fingerprint}"} for w in rows]
        answer = await self._ask(ctx, "pick_waiting_machine", questions(LAN_QUESTIONS_FILE)["pick_waiting_machine"]["text"], options)
        if answer is None:
            return ProbeResult(ok=False, error="the owner did not answer in time")
        self.picks.add(answer)
        self._owner(ctx.store, "owner_pick", {"waiting": answer})  # the OWNER's pick, as an OWNER event: pair_offer_start checks for it
        return ProbeResult(ok=True, facts=[Fact(target_id=ctx.target.id, key="owner.pick.waiting", value=answer, status=FactStatus.tested, created_by=CreatedBy.human,
                                                 evidence=[Evidence(provider="human", probe="ask_owner", event_id=ctx.event_id, confidence=1.0)])])

    def require_owner_pick(self, waiting_id: str) -> str | None:
        """The refusal text, or None when the OWNER picked this machine in this run."""
        if waiting_id not in self.picks:
            return "refused: the owner has not picked this computer. Ask the owner (ask_owner) which waiting computer to pair; a code is made only for one the owner picked."
        return None

    async def _offer(self, args: dict, ctx: ToolContext) -> ProbeResult:
        wid = str(args.get("machine", ""))
        refusal = self.require_owner_pick(wid)  # FIRST: nothing below runs without an OWNER pick
        if refusal:
            return ProbeResult(ok=False, error=refusal)
        snap = self.offer_for_waiting(wid, "agent")
        view = snap.get("pairing") or {}
        return ProbeResult(ok=True, facts=[_fact(ctx.target.id, "lan.pair_offer_started", {"waiting": wid, "offer": view.get("offer"), "valid_s": 120}, "pair_offer_start", "coordinator.pairing", event_id=ctx.event_id,
                                                  confidence=1.0)])


def check_model(profile) -> dict:
    """Is the configured model reachable right now? A typed answer with a plain reason; never raises."""
    kind = profile.kind.value
    try:
        if kind == "openai_compat":
            r = httpx.get(profile.endpoint.rstrip("/") + "/models", timeout=3.0)
            return {"available": r.status_code < 500, "profile": profile.name, "reason": "" if r.status_code < 500 else f"the model server answered {r.status_code}"}
        if kind == "claude_cli":
            ok = shutil.which("claude") is not None
            return {"available": ok, "profile": profile.name, "reason": "" if ok else "the command-line tool is not installed on the coordinator"}
    except Exception as exc:
        return {"available": False, "profile": profile.name, "reason": f"the model server did not answer ({type(exc).__name__})"}
    return {"available": False, "profile": profile.name, "reason": f"unsupported model kind {kind}"}


def write_owner_event(store, type: str, payload: dict, run_id: str | None) -> None:
    """An OWNER event in the run's own log (written from the coordinator's loop thread)."""
    store.append_event(Event(actor=Actor.human, type=type, payload=payload, run_id=run_id, tag="CLAUDE-CODE" if payload.get("by") == "claude-code" else "OWNER"))


def new_target(store, kind: TargetKind, handle: str) -> Target:
    return store.put_target(Target(kind=kind, ring=0, handle=handle))


__all__ = ["AnalysisAdapter", "LanAdapter", "OptionsAdapter", "PROBE_TOOLS", "check_model", "new_target", "questions", "write_owner_event", "Path"]

"""Tools of protocol-draft (skill `protocol-draft`): the model reads the network, proposes each machine's environment and the team, marks what it assumes, applies the owner's correction as typed
edits and asks the owner only when a closed decision is the owner's. CODE validates (the hard limits in `draft.py`), runs, records. It never picks an environment, a topology or an
answer: a refused proposal is a typed refusal the model can read, and every assumption it makes carries what it rests on.

Nothing here installs or writes on a node: the output of an approved draft is data (`draft.approved_output`)."""

from __future__ import annotations

from typing import Any

from envrec.adapters import ToolContext
from envrec.contracts import CreatedBy, Evidence, Fact, FactStatus, ProbeResult, TargetKind, ToolSpec
from envrec_coordinator import draft as D
from envrec_coordinator import options as O
from envrec_coordinator.agent import ANSWER_PREFIX, BaseAdapter, _fact, _spec



def _props(**props) -> dict:
    """The argument form of a tool as the model sees it. A value is `str` (free text), a list of allowed values (`enum`: the model picks one) or `("list", allowed)` (any of them, as a JSON list)."""
    out = {}
    for name, (kind, desc) in props.items():
        if isinstance(kind, tuple):
            out[name] = {"type": "array", "items": {"type": "string", "enum": list(kind[1])}, "description": desc}
        elif isinstance(kind, list):
            out[name] = {"type": "string", "enum": list(kind), "description": desc}
        else:
            out[name] = {"type": "string", "description": desc}
    return {"type": "object", "properties": out, "required": list(out)}


class DraftAdapter(BaseAdapter):
    def __init__(self, *, network, store, owner_events, correction: str | None = None, round_: int = 1, catalog: dict | None = None, harnesses: dict | None = None) -> None:
        super().__init__(owner_events=owner_events)
        self.network, self.store = network, store  # network() -> [{"node_id","name","stored"}]; store: the DraftStore
        self.correction, self.round = correction, round_
        self.catalog, self.harnesses = catalog, harnesses
        self.machines: dict[str, dict] = {}  # id -> the summary the view shows
        self.facts: dict[str, dict] = {}  # id -> the `machine.*` facts of its stored analysis
        self.c = D.Constraints()
        self.edits: list[dict] = []
        self.drafts: dict[str, dict] = {}
        self.net: dict | None = None
        self.summary = ""
        self.final: dict | None = None
        self.choices: dict[str, dict] = {}  # machine id -> its closed set of valid environments (rebuilt whenever the owner states something)
        self._refs_known: set[str] = set()  # fact keys an assumption may name (kept current as facts are written)

    # ---- the menu ------------------------------------------------------------------------------------------------------------------------------

    def _all_choices(self) -> list[str]:
        return [c for m in self._live() for c in self.choices.get(m, {}).get("choices", [])]

    def tool_specs(self) -> list[ToolSpec]:
        k = [TargetKind.network]
        v = D.vocab()
        need = ["network.loaded"]
        refs = D.valid_refs(set(self._refs_known))
        res_tokens = list(v["resources"])
        by = {
            "load_network": _spec("load_network", "Read the paired machines and their stored analyses (nothing is probed).", kinds=k, produces=["network.loaded", "network.none"]),
            "ask_owner": _spec("ask_owner", "Ask the owner one closed question.", kinds=k, requires=need,
                               schema=_props(question=(sorted(D.questions()), "the question to put to the owner"))),
            "apply_edit": _spec("apply_edit", "Record one thing the owner said as a typed edit.", kinds=k, requires=need, repeatable=True,
                                schema=_props(kind=(sorted(v["edit_kinds"]), "what the owner changed"), machine=(sorted(self.machines) + ["none"], "the machine it is about, or none"),
                                              value=(str, "resources: " + ", ".join(res_tokens) + "; sensitivity: " + ", ".join(v["sensitivity"]) + "; topology: " + ", ".join(v["topologies"])
                                                     + "; freedom: " + ", ".join(v["freedom"]) + "; rhythm: " + ", ".join(v["rhythm"]) + "; tasks: free words; director, exclude_machine: none"),
                                              quote=(str, "the owner's own words that say it, copied exactly"))),
            "draft_machine": _spec("draft_machine", "Draft one machine's environment from its valid choices.", kinds=k, requires=need, repeatable=True, produces=["draft.machines_covered"],
                                   schema=_props(choice=(self._all_choices() or ["(none)"], "the machine, environment, access and model in one id; each is valid on its machine now"),
                                                 resources=(("list", res_tokens), "what the owner puts on this machine"), because=(str, "why, one sentence, in English"),
                                                 rests_on=(("list", refs), "what this proposal rests on, at least one"))),
            "draft_network": _spec("draft_network", "Draft the team: tasks, who directs, freedom and rhythm.", kinds=k, requires=need, repeatable=True, produces=["draft.network"],
                                   schema=_props(tasks=(str, "what the network is for, in English"), team=(D.team_choices(sorted(self.machines), self.c) or ["(none)"], "the topology and who directs, in one id"),
                                                 freedom=(list(v["freedom"]), "how much the agent decides alone"), rhythm=(list(v["rhythm"]), "when the agent works"),
                                                 because=(str, "why, one sentence, in English"), rests_on=(("list", refs), "what this proposal rests on, at least one"))),
            "finish_draft": _spec("finish_draft", "State what is proposed and still assumed; ends the draft.", kinds=k, requires=need, produces=["draft.finished", "draft.summary"],
                                  schema=_props(summary=(str, "what you propose and what is still assumed, in English"))),
        }
        return [by[n] for n in ("load_network", "draft_machine", "draft_network", "apply_edit", "ask_owner", "finish_draft")]

    async def run(self, tool: str, args: dict[str, Any], ctx: ToolContext) -> ProbeResult:
        try:
            handler = {"load_network": self._load, "ask_owner": self._ask_owner, "apply_edit": self._apply_edit, "draft_machine": self._draft_machine, "draft_network": self._draft_network,
                       "finish_draft": self._finish}.get(tool)
            if handler is None:
                return ProbeResult(ok=False, error=f"unknown tool {tool}")
            self._refs_known |= self._known(ctx)
            res = await handler(args, ctx)
            self._refs_known |= {f.key for f in res.facts}
            return res
        except Exception as exc:
            return ProbeResult(ok=False, error=f"{type(exc).__name__}: {str(exc)[:200]}")

    # ---- helpers -------------------------------------------------------------------------------------------------------------------------------

    def _known(self, ctx: ToolContext) -> set[str]:
        return {f.key for f in ctx.store.current_facts(ctx.target.id) if f.status != FactStatus.unknown}

    def _live(self) -> list[str]:
        return [m for m in sorted(self.machines) if m not in self.c.excluded]

    @staticmethod
    def _refuse(errs: list[dict]) -> ProbeResult:
        text = "refused: " + "; ".join(f"{e['rule']}: {e['reason']}" for e in errs)
        return ProbeResult(ok=False, error=text if len(text) <= 300 else text[:296] + "...")

    def _sens(self, mid: str) -> tuple[str, str | None]:
        val, src = self.c.sensitivity_for(mid)
        return (val, src) if val else ("internal", None)

    def _covered(self) -> bool:
        return bool(self._live()) and all(m in self.drafts for m in self._live())

    def _coverage_fact(self, ctx: ToolContext) -> Fact:
        return _fact(ctx.target.id, "draft.machines_covered", self._covered(), "draft_machine", "envrec_coordinator.draft_agent", created_by=CreatedBy.loop, event_id=ctx.event_id, confidence=1.0)

    def still_to_draft(self) -> dict:
        """What is still missing from the draft, derived from the state alone: the machines (not left out by the owner) without a draft, whether the team is drafted, and what ending the draft still
        lacks. It states facts of the state; it names no action and recommends none."""
        left = [m for m in self._live() if m not in self.drafts]
        return {"machines_without_draft": left, "team_drafted": self.net is not None,
                "still_needed_to_end": [f"a draft of machine {m}" for m in left] + ([] if self.net is not None else ["a draft of the team"])}

    def _pending_fact(self, ctx: ToolContext, probe: str) -> Fact:
        return _fact(ctx.target.id, "draft.pending", self.still_to_draft(), probe, "envrec_coordinator.draft_agent", created_by=CreatedBy.loop, event_id=ctx.event_id, confidence=1.0,
                     limitations=["derived from the current drafts and the machines the owner kept; it is a record of the state, not advice"])

    def _rebuild_choices(self) -> None:
        for mid in self.machines:
            self.choices[mid] = D.machine_choices(mid, self.machines[mid], self.facts[mid], self.c, self.catalog, self.harnesses)

    def _machine_fact(self, ctx: ToolContext, mid: str, probe: str = "load_network") -> Fact:
        src = f"stored analysis {self.machines[mid].get('analysis_run')}"
        ch = self.choices[mid]
        summ = {**self.machines[mid], "valid_choices": ch["choices"], "local_models_that_fit": ch["local_total"], "local_models_listed": D.LOCAL_SHOWN}
        return Fact(target_id=ctx.target.id, key=f"network.machine.{D.key_id(mid)}", value=summ, status=FactStatus.tested, created_by=CreatedBy.probe,
                    evidence=[Evidence(provider=src, probe=probe, event_id=ctx.event_id, confidence=0.9, limitations=["read from the stored analysis, not probed again; valid_choices is what code lets through now"])])

    # ---- load_network --------------------------------------------------------------------------------------------------------------------------

    async def _load(self, args: dict, ctx: ToolContext) -> ProbeResult:
        rows = [r for r in self.network() if r.get("stored")]
        if not rows:
            return ProbeResult(ok=True, facts=[_fact(ctx.target.id, "network.none", {"paired_without_analysis": len(self.network())}, "load_network", "coordinator.registry", event_id=ctx.event_id, confidence=1.0)])
        out: list[Fact] = []
        for r in rows:
            mid = r["node_id"]
            self.facts[mid] = D.facts_of(r["stored"])
            self.machines[mid] = D.machine_summary(mid, r["name"], r["stored"])
        self._rebuild_choices()
        out += [self._machine_fact(ctx, mid) for mid in sorted(self.machines)]
        out.append(_fact(ctx.target.id, "network.loaded", {"machines": sorted(self.machines)}, "load_network", "coordinator.registry", event_id=ctx.event_id, confidence=1.0))
        out.append(self._pending_fact(ctx, "load_network"))
        for key, e in D.profile_facts(self.store.profile()).items():  # context only: the model weighs it, no code forces it
            out.append(Fact(target_id=ctx.target.id, key=key, value={k: e[k] for k in ("kind", "machine", "machine_name", "value", "quote", "round")}, status=FactStatus.tested, created_by=CreatedBy.human,
                            evidence=[Evidence(provider="owner profile", probe="load_network", event_id=ctx.event_id, confidence=0.7, limitations=["the owner said this in an earlier round; it is a prior, not a rule"])]))
        prev = self.store.current()
        if prev and prev.get("machines"):
            out.append(Fact(target_id=ctx.target.id, key="draft.previous", value={"round": prev["round"], "status": prev.get("status"), "network": prev["network"],
                                                                                    "machines": {m: {"environment": d["environment"], "resources": d["resources"]} for m, d in prev["machines"].items()}},
                            status=FactStatus.hypothesis, created_by=CreatedBy.probe, evidence=[Evidence(provider="previous draft", probe="load_network", event_id=ctx.event_id, confidence=0.5,
                                                                                                        limitations=["an earlier proposal, itself made of assumptions"])]))
        if self.correction:
            out.append(Fact(target_id=ctx.target.id, key="owner.correction.pending", value={"text": self.correction[:600], "round": self.round}, status=FactStatus.tested, created_by=CreatedBy.human,
                            evidence=[Evidence(provider="human", probe="owner_correction", event_id=ctx.event_id, confidence=1.0)]))
        return ProbeResult(ok=True, facts=out)

    # ---- the owner -------------------------------------------------------------------------------------------------------------------------------

    async def _ask_owner(self, args: dict, ctx: ToolContext) -> ProbeResult:
        qid = str(args.get("question", ""))
        q = D.questions().get(qid)
        if q is None:
            return ProbeResult(ok=False, error=f"unknown question {qid!r}; choose one of {sorted(D.questions())}")
        if q.get("dynamic") == "machines":  # shown unordered (by id), each with the facts it has: no "best"
            options = [{"id": m, "label": s["name"], "detail": f"{s['os']} {s['arch']} · RAM {s['ram_total_gib']} GiB · modelos en: {s['model_path']} · accesos: {', '.join(s['accesses'])}"}
                       for m, s in sorted(self.machines.items()) if m not in self.c.excluded]
        else:
            options = q["options"]
        if len(options) < 2:
            return ProbeResult(ok=False, error=f"there is nothing to choose between for {qid}")
        if self.pending is not None:
            return ProbeResult(ok=False, error="a question is already waiting for the owner")
        answer = await self._ask(ctx, qid, q["text"], options)
        if answer is None:
            return ProbeResult(ok=False, error="the owner did not answer in time")
        by = self.last_answered_by
        self._owner(ctx.store, "owner_answer", {"question": qid, "option": answer, **({"by": by} if by != "owner" else {})})
        ref = f"{ANSWER_PREFIX}{qid}"
        if qid in ("freedom", "rhythm", "topology"):
            setattr(self.c, qid, answer)
        elif qid == "director_machine":
            self.c.director = answer
        else:
            self.c.work_sensitivity = answer
        self.c.source[(qid if qid != "director_machine" else "director", None if qid != "director_machine" else answer)] = ref
        if qid == "work_sensitivity":
            self.c.source[("work_sensitivity", None)] = ref
        f = Fact(target_id=ctx.target.id, key=ref, value=answer, status=FactStatus.tested, created_by=CreatedBy.human,
                 evidence=[Evidence(provider="human" if by == "owner" else "operator (claude-code) for the owner", probe="ask_owner", event_id=ctx.event_id, confidence=1.0,
                                    limitations=[] if by == "owner" else ["the operator answered on the owner's behalf in a simulated-local verification, authorized in chat; it is not the owner's answer"])])
        return ProbeResult(ok=True, facts=[f] + self._revalidate(ctx))

    async def _apply_edit(self, args: dict, ctx: ToolContext) -> ProbeResult:
        if len(self.edits) >= D.MAX_EDITS:
            return ProbeResult(ok=False, error=f"at most {D.MAX_EDITS} edits in one round")
        code, res = D.edit_error(str(args.get("kind", "")), str(args.get("machine", "")), str(args.get("value", "")), str(args.get("quote", "")), self.correction, self.machines)
        if code:
            return ProbeResult(ok=False, error=f"refused: {code}: {res}")
        self.edits.append(res)
        ref = f"owner.edit.{len(self.edits)}"
        D.apply_edit(self.c, res, ref)
        f = Fact(target_id=ctx.target.id, key=ref, value=res, status=FactStatus.tested, created_by=CreatedBy.human,
                 evidence=[Evidence(provider="human (quoted)", probe="apply_edit", event_id=ctx.event_id, confidence=0.9,
                                    limitations=["the model typed this edit from the owner's words; the guard checked that the quote is in what the owner wrote"])])
        return ProbeResult(ok=True, facts=[f] + self._revalidate(ctx))

    def _revalidate(self, ctx: ToolContext) -> list[Fact]:
        """A new owner statement can make an earlier proposal invalid: that proposal is marked stale (the model drafts it again), never silently kept. The closed sets are rebuilt from the new limits."""
        out: list[Fact] = []
        self._rebuild_choices()
        for mid, d in list(self.drafts.items()):
            env = d["environment"]
            errs, _ = D.machine_errors(self.machines[mid], self.facts[mid], self.c, subtype=env["subtype"], access=env["access"], model=env.get("model") or "none", resources=d["resources"],
                                       sensitivity=self._sens(mid)[0], catalog=self.catalog, harnesses=self.harnesses)
            if mid in self.c.excluded or errs:
                del self.drafts[mid]
                why = "the owner left this machine out" if mid in self.c.excluded else "; ".join(e["rule"] for e in errs)
                out.append(_fact(ctx.target.id, f"draft.machine.{D.key_id(mid)}", {"stale": why}, "apply_edit", "envrec_coordinator.draft_agent", status=FactStatus.hypothesis, created_by=CreatedBy.loop, event_id=ctx.event_id, confidence=1.0))
        if self.net is not None:
            errs = D.network_errors(self.net["topology"], self.net["director"], sorted(self.machines), self.c, freedom=self.net["freedom"], rhythm=self.net["rhythm"])
            if errs:
                self.net = None
                out.append(_fact(ctx.target.id, "draft.network", {"stale": "; ".join(e["rule"] for e in errs)}, "apply_edit", "envrec_coordinator.draft_agent", status=FactStatus.hypothesis, created_by=CreatedBy.loop, event_id=ctx.event_id, confidence=1.0))
        out += [self._machine_fact(ctx, mid, "apply_edit") for mid in sorted(self.machines)]
        out.append(self._coverage_fact(ctx))
        out.append(self._pending_fact(ctx, "apply_edit"))
        return out

    # ---- the draft -------------------------------------------------------------------------------------------------------------------------------

    @staticmethod
    def _trunc(text: str) -> ProbeResult:
        return ProbeResult(ok=False, error=text if len(text) <= 300 else text[:296] + "...")

    @staticmethod
    def _element(name: str, value, owner_ref: str | None, refs: list[str]) -> dict:
        if owner_ref:
            return {"element": name, "value": value, "status": "owner", "source": owner_ref}
        return {"element": name, "value": value, "status": "assumption", "rests_on": refs}

    async def _draft_machine(self, args: dict, ctx: ToolContext) -> ProbeResult:
        cid = str(args.get("choice", "")).strip()
        parsed = D.parse_choice(cid)
        if parsed is None or cid not in self._all_choices():
            mine = self.choices.get(parsed[0], {}).get("choices") if parsed and parsed[0] in self.machines else None
            return self._trunc(f"refused: unknown_choice: {cid!r} is not valid now; valid" + (f" for {parsed[0]}: {mine}" if mine else f": {self._all_choices()}"))
        mid, subtype, access, model = parsed
        resources = D.split_list(args.get("resources", ""))
        because = " ".join(str(args.get("because", "")).split())[:240]
        if not because:
            return ProbeResult(ok=False, error="say why, in one sentence")
        sens, sens_src = self._sens(mid)
        errs, checks = D.machine_errors(self.machines[mid], self.facts[mid], self.c, subtype=subtype, access=access, model=model, resources=resources, sensitivity=sens,
                                        catalog=self.catalog, harnesses=self.harnesses)
        if errs:
            return self._refuse(errs)
        code, refs = D.refs_error(args.get("rests_on", ""), D.valid_refs(self._known(ctx)))
        if code:
            return self._trunc(f"refused: {code}: {refs}")
        owner_res = self.c.source.get(("resources", mid)) if mid in self.c.resources else None
        acc = next(a for a in D.accesses(self.facts[mid]) if a["id"] == access)
        option_id = model if subtype == "own_harness" else acc.get("option_id")
        els = [self._element("subtype", subtype, None, refs), self._element("access", access, None, refs)]
        if subtype == "own_harness":
            els.append(self._element("model", model, None, refs))
            els.append({"element": "harness", "value": None, "status": "assumption", "rests_on": ["default:no_harness_catalog"]})
        els.append(self._element("resources", resources, owner_res, refs))
        els.append(self._element("sensitivity", sens, sens_src, ["default:internal_sensitivity"]))
        rec = {"machine_id": mid, "name": self.machines[mid]["name"], "environment": {"subtype": subtype, "access": access, "model": None if model in ("", "none") else model, "harness": None,
                                                                                    "option_id": option_id, "cloud": acc["cloud"]},
               "resources": resources, "sensitivity": {"value": sens, "status": "owner" if sens_src else "assumption", "source": sens_src or "default:internal_sensitivity"},
               "elements": els, "checks": checks, "because": because, "status": D.PROVISIONAL}
        self.drafts[mid] = rec
        f = Fact(target_id=ctx.target.id, key=f"draft.machine.{D.key_id(mid)}", value=rec, status=FactStatus.hypothesis, created_by=CreatedBy.model,
                 evidence=[Evidence(provider="model", probe="draft_machine", event_id=ctx.event_id, confidence=0.5, limitations=["a proposal made of assumptions; the checks inside it were measured here by code"])])
        return ProbeResult(ok=True, facts=[f, self._coverage_fact(ctx), self._pending_fact(ctx, "draft_machine")])

    async def _draft_network(self, args: dict, ctx: ToolContext) -> ProbeResult:
        team = str(args.get("team", "")).strip()
        valid = D.team_choices(sorted(self.machines), self.c)
        if team not in valid:
            return self._trunc(f"refused: unknown_choice: {team!r} is not valid now; valid: {valid}")
        topology, director = D.parse_team(team)
        freedom, rhythm = (" ".join(str(args.get(k, "")).split()) for k in ("freedom", "rhythm"))
        tasks = " ".join(str(args.get("tasks", "")).split())[:160]
        because = " ".join(str(args.get("because", "")).split())[:240]
        if not tasks or not because:
            return ProbeResult(ok=False, error="say the tasks and why, in English")
        errs = D.network_errors(topology, director, sorted(self.machines), self.c, freedom=freedom, rhythm=rhythm)
        if self.c.tasks and tasks != self.c.tasks:
            errs.append({"rule": "contradicts_owner", "reason": f"the owner said the tasks are {self.c.tasks!r}"})
        if errs:
            return self._refuse(errs)
        src = self.c.source
        owner = {"tasks": src.get(("tasks", None)) if self.c.tasks else None, "topology": src.get(("topology", None)) if self.c.topology else None,
                 "freedom": src.get(("freedom", None)) if self.c.freedom else None, "rhythm": src.get(("rhythm", None)) if self.c.rhythm else None,
                 "director": next((v for (k, _m), v in src.items() if k == "director"), None) if self.c.director else None}
        code, refs = D.refs_error(args.get("rests_on", ""), D.valid_refs(self._known(ctx)))
        if code:
            return self._trunc(f"refused: {code}: {refs}")
        els = [self._element("tasks", tasks, owner["tasks"], refs), self._element("topology", topology, owner["topology"], refs)]
        if director:
            els.append(self._element("director", director, owner["director"], refs))
        els += [self._element("freedom", freedom, owner["freedom"], refs), self._element("rhythm", rhythm, owner["rhythm"], refs)]
        self.net = {"tasks": tasks, "topology": topology, "director": director, "freedom": freedom, "rhythm": rhythm, "elements": els, "because": because, "status": D.PROVISIONAL}
        f = Fact(target_id=ctx.target.id, key="draft.network", value=self.net, status=FactStatus.hypothesis, created_by=CreatedBy.model,
                 evidence=[Evidence(provider="model", probe="draft_network", event_id=ctx.event_id, confidence=0.5, limitations=["a proposal made of assumptions"])])
        return ProbeResult(ok=True, facts=[f, self._pending_fact(ctx, "draft_network")])

    async def _finish(self, args: dict, ctx: ToolContext) -> ProbeResult:
        """Offered once the exit's evidence is in; it still re-checks the whole draft, so an edit that landed after a proposal cannot slip through."""
        summary = " ".join(str(args.get("summary", "")).split())[:400]
        if not summary:
            return ProbeResult(ok=False, error="say what you propose and what is still assumed")
        if self.net is None or not self._covered():
            return ProbeResult(ok=False, error="refused: draft_incomplete: every machine the owner kept needs its draft, and the team needs one")
        errs = D.network_errors(self.net["topology"], self.net["director"], sorted(self.machines), self.c, freedom=self.net["freedom"], rhythm=self.net["rhythm"])
        if errs:
            return self._refuse(errs)
        self.summary = summary
        self.final = self.doc()
        done = _fact(ctx.target.id, "draft.finished", True, "finish_draft", "envrec_coordinator.draft_agent", created_by=CreatedBy.loop, event_id=ctx.event_id, confidence=1.0,
                     limitations=["the model declared the draft finished and the guard accepted it; the owner has NOT approved it"])
        text = _fact(ctx.target.id, "draft.summary", summary, "finish_draft", "model", status=FactStatus.hypothesis, created_by=CreatedBy.model, event_id=ctx.event_id, confidence=0.5)
        return ProbeResult(ok=True, facts=[done, text])

    def doc(self) -> dict:
        return {"round": self.round, "machines": dict(sorted(self.drafts.items())), "network": self.net, "excluded": sorted(self.c.excluded), "edits": list(self.edits), "summary": self.summary,
                "status": "proposed"}

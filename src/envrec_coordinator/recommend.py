"""The recommendation for work no catalog task type names (protocol-recommend, rung 1: known data). CODE builds it from the same router the task will use: sensitivity first (a confidential task never
sees a cloud access), then the written rule, so the recommended default is what the rule picks and the runner-up is what it picks when the default's environment is left out. A MODEL never ranks here. What
research found is only ever a lead (a hypothesis, never the default). The owner answers ONE chat question bound to the plan hash; his answer is an OWNER event and only then is a task created, whose route he
approves in turn.

Actors: the rule's choice is ROUTER (a `route_decision` is not recorded here, `explain` records nothing), the question and the refusals are GUARD, the answer is OWNER."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from envrec.contracts import TargetKind
from envrec.loop import RunConfig
from envrec.protocols import ProtocolSet
from envrec.skills.discover import library
from envrec.store import Store
from envrec_coordinator import agent as A
from envrec_coordinator import recommend_strings as RC
from envrec_coordinator.admission.suite import DATA_ROOT
from envrec_coordinator.recommend_agent import NONE_FITS, RecommendAdapter
from envrec_coordinator.research import Researcher, real_fetch
from envrec_coordinator.runs import HarnessLoop
from envrec_coordinator.router import Refusal, Router
from envrec_coordinator.router import strings as RS
from envrec_coordinator.router.catalog import knows_task_type
from envrec_coordinator.router.model import EntryChat, Exclude, RouteDecision, Task
from envrec_net import envrun as E


@dataclass
class Option:
    id: str  # use_default | use_runner | clear_default
    kind: str  # use | clear_and_use
    env: str
    route_key: str
    task_type: str
    cost_class: str
    cloud: bool
    access: str
    machine: str
    evidence: str
    tags: list[str] = field(default_factory=list)


@dataclass
class Proposal:
    task_type: str
    domains: list[str]
    sensitivity: str
    unclassified: bool
    options: list[Option]
    leads: list[dict]
    setup: list[tuple[str, str, str]]  # (environment id, machine id, reason text)
    refusal: Refusal | None
    hash: str
    measure: list[str] = field(default_factory=list)  # local combinations that wait for a measurement (a confidential request goes only to measured ones)
    type_why: str = ""  # why the harness model chose the type, in its words (shown to the owner as the model's, never as a fact)


def generic_type() -> str:
    return str(E.load_policy().get("recommend", {}).get("unclassified_task_type", "text"))


def max_leads() -> int:
    return int(E.load_policy().get("recommend", {}).get("max_leads_shown", 5))


def normalize_domain(tags: Any, description: str | None) -> list[str] | None:
    """The domain tags of a request: the ones given, else the short description as one tag. None when they are not plain tags (the task text itself is never one)."""
    raw = tags if isinstance(tags, list) and tags else ([description] if description else [])
    try:
        return Task(task_id="x", task_type="x", entry_chat=EntryChat(kind="local"), created_by="OWNER", payload_ref="x", domain=[str(t) for t in raw]).domain
    except ValueError:
        return None


def _digest(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


class RecommendTools:
    """Mixed into `Services`: it uses the router, the policy, the catalog, the profile and the harness the services already hold."""

    def _twin(self, policy) -> Router:
        return Router(envs=self.envs, facts=self.facts, policy=policy, log=lambda t, p, a: None, outputs=self.outputs, instructions=self.router.instructions, evidence=self.router.evidence)

    def _rec_task(self, ch, task_type: str, domains: list[str], sensitivity: str, tid: str, exclude: list[str] | None = None) -> Task:
        return Task(task_id=tid, task_type=task_type, sensitivity=sensitivity, entry_chat=self._chat(ch), created_by="OWNER", payload_ref="recommend", domain=domains,
                    exclude=Exclude(environment_ids=exclude) if exclude else None)

    def _option(self, oid: str, kind: str, d: RouteDecision, task_type: str) -> Option:
        entry = self.envs[d.route.environment_id]
        cost = next((s.values.get(d.route.key) for s in d.rule_trace if s.step == "cost_class"), None) or next(a["cost_class"] for a in entry["accesses"] if a["id"] == d.route.access_id)
        fit = next((s for s in d.rule_trace if s.step == "domain_fit"), None)
        return Option(oid, kind, entry["id"], d.route.key, task_type, cost, E.is_cloud(entry, d.route.access_id), d.route.access_id, d.route.machine_id, d.evidence.get("text", RS.EVIDENCE_NONE_LINE),
                      list(fit.values.get(d.route.key, [])) if fit else [])

    def build_proposal(self, ch, *, domains: list[str], sensitivity: str, task_type: str | None = None, unclassified: bool = True, type_why: str = "") -> Proposal:
        tt = task_type or generic_type()
        options: list[Option] = []
        refusal = None
        res = self.router.explain(self._rec_task(ch, tt, domains, sensitivity, "rec-default"))
        if isinstance(res, RouteDecision):
            options.append(self._option("use_default", "use", res, tt))
            alt = self.router.explain(self._rec_task(ch, tt, domains, sensitivity, "rec-runner", exclude=[res.route.environment_id]))
            if isinstance(alt, RouteDecision):
                options.append(self._option("use_runner", "use", alt, tt))
        else:
            refusal = res
        if sensitivity != "confidential" and not any(o.kind == "clear_and_use" for o in options):  # a cloud access is never reached by a confidential task, so there is nothing to clear for one
            review = {k for k, e in self.envs.items() if e.get("automated_use") == "to review" and k not in self.policy.automated_use_cleared}
            if review:
                wide = copy.copy(self.policy)
                wide.automated_use_cleared = set(self.policy.automated_use_cleared) | review
                best = self._twin(wide).explain(self._rec_task(ch, tt, domains, sensitivity, "rec-clear"))
                if isinstance(best, RouteDecision) and best.route.environment_id in review and best.route.environment_id not in {o.env for o in options}:
                    opt = self._option("clear_default" if not options else "clear_extra", "clear_and_use", best, tt)
                    options = [opt] if not options else [*options, opt]
                    refusal = None
        setup: list[tuple[str, str, str]] = []
        left = (refusal.details.get("environments") if isinstance(refusal, Refusal) else None) or (res.left_out if isinstance(res, RouteDecision) else {})
        for tag, why in sorted(left.items()):
            env_id, _, mid = tag.partition("@")
            env_id = env_id.split(" via ")[0]
            entry = self.envs.get(env_id)
            if entry is None or tt not in entry.get("task_types", []) or any(o.env == env_id for o in options):
                continue
            if sensitivity == "confidential" and E.is_cloud(entry):
                continue  # a confidential request never uses a cloud access, so there is nothing to set up or clear for it
            if why in (RS.REASONS["not_installed"], RS.REASONS["server_not_running"], RS.REASONS["not_signed_in"], RS.REASONS["automated_use_unreviewed"]) or why.startswith(("access missing", RS.REASONS["no_access"])):
                costs = ", ".join(sorted({a["cost_class"] for a in entry.get("accesses", [])}))
                hint = f"; {RC.REC_CLEAR_HINT.format(id=env_id)}" if why == RS.REASONS["automated_use_unreviewed"] else ""
                setup.append((env_id, mid, why + f" (cost class {costs})" + hint + f"; {self._install_note(env_id, entry)}"))
        tagset = set(domains)
        leads = [{"id": k, "title": e.get("title", k), "domains": e.get("domains", []), "license": (e.get("license") or {}).get("spdx", "unknown"), "state": e.get("evidence_state"), "source": e.get("source_url", ""), "install": self._install_note(k, e)}
                 for k, e in sorted(self.catalog.items()) if e.get("origin") == "researched" and tagset & set(e.get("domains", []))][:max_leads()]
        h = _digest(tt, domains, sensitivity, [(o.id, o.route_key, o.kind) for o in options], [lead["id"] for lead in leads], [(a, b) for a, b, _ in setup], refusal.code if refusal else None)[:12]
        measure = [self._key_text(k) for k, why in (refusal.details.get("dropped", {}) if isinstance(refusal, Refusal) else {}).items() if why == RS.REASONS["provisional"]]
        return Proposal(tt, domains, sensitivity, unclassified, options, leads, setup, refusal, h, measure, type_why)

    def recommendation_text(self, p: Proposal) -> str:
        what = f"a \"{', '.join(p.domains)}\" task" if p.domains else "this task"
        lines = [RC.REC_HEAD.format(what=what, level=p.sensitivity)]
        if p.unclassified:
            lines.append(RC.REC_AS_TYPE.format(task_type=p.task_type, domain=", ".join(p.domains)))
        elif p.type_why:
            lines.append(RC.REC_TYPE_CHOSEN.format(task_type=p.task_type, why=p.type_why))
        for o in p.options:
            where = "cloud" if o.cloud else "local"
            if o.kind == "clear_and_use":
                lines.append(RC.REC_DEFAULT_CLEAR.format(env=o.env, machine=self._name(o.machine), cost_class=o.cost_class, where=where) + f" {o.evidence}.")
            elif o.id == "use_default":
                lines.append(RC.REC_DEFAULT.format(route=self._key_text(o.route_key), cost_class=o.cost_class, where=where, evidence=o.evidence[0].upper() + o.evidence[1:], access=o.access))
            else:
                lines.append(RC.REC_RUNNER.format(route=self._key_text(o.route_key), cost_class=o.cost_class, where=where, evidence=o.evidence[0].upper() + o.evidence[1:]))
            if o.tags:
                lines.append("  " + RC.REC_DOMAIN_FIT.format(tags=", ".join(o.tags)))
        if p.refusal is not None:
            lines.append(RC.REC_NOTHING.format(what=what, reason=p.refusal.reason))
        if p.measure:
            lines.append(RC.REC_MEASURE_HINT.format(task_type=p.task_type, keys="; ".join(p.measure)))
        if p.setup:
            lines.append(RC.REC_SETUP_HEAD)
            lines += [RC.REC_SETUP.format(env=e, machine=self._name(m) if m else "-", reason=r) for e, m, r in p.setup]
        if p.leads:
            lines.append(RC.REC_LEADS_HEAD.format(n=len(p.leads)))
            lines += [RC.REC_LEAD.format(title=x["title"], id=x["id"], domains=", ".join(x["domains"]) or "no tag", license=x["license"], state=x["state"], source=x["source"]) + f" Install: {x['install']}" for x in p.leads]
        if p.options:
            lines.append(RC.REC_ASK.format(hash=p.hash))
        return "\n".join(lines)

    # ---- the question and its answer -------------------------------------------------------------------------------------------------------------

    def recommend(self, ch, *, text: str, description: str, domain, sensitivity: str, check=None, categories=None) -> dict:
        """protocol-recommend for a request no catalog type names. Rung 1, known data: the proposal from the catalog and the router. When that names no environment tagged for the domain and a harness model can be used,
        rungs 2-3 run first (the model chooses the type from the closed list and looks at public sources, as leads). The last rung is ONE chat question bound to the plan hash."""
        tags = normalize_domain(domain, description)
        if tags is None:
            return self._out(False, RC.REC_BAD_DOMAIN)
        if not knows_task_type(self.envs, generic_type()):
            return self._out(False, RC.REC_NO_TYPE.format(task_type=generic_type()))
        ctx = {"text": text, "tags": tags, "sensitivity": sensitivity, "check": check, "categories": list(categories or []), "note": ""}
        rung1 = self.build_proposal(ch, domains=tags, sensitivity=sensitivity)
        if any(o.tags for o in rung1.options):  # known data names an environment tagged for the domain: the web is not needed
            return self._present(ch, ctx)
        why = self._research_blocked(ctx)
        if why:
            ctx["note"] = RC.REC_RESEARCH_SKIPPED.format(why=why)
            return self._present(ch, ctx)
        return self._start_research(ch, ctx, rung1)

    # ---- rungs 2-3: the harness model, public sources ----------------------------------------------------------------------------------------

    def _install_note(self, env_id: str, entry: dict) -> str:
        """What the owner is told about installing an entry: either that the harness can (mas_install_tool) or the typed state (no_installer / unverified_entry) with its reason. Code decides, from the entry's data."""
        from envrec_coordinator.tool_plan import install_state
        state, why = install_state(entry, set(E.load_policy().get("permissive_licenses", [])))
        return RC.REC_CAN_INSTALL.format(id=env_id) if state == "installable" else f"{state}: {why}"

    def _recommend_model(self):
        m = getattr(self, "recommend_model", None)
        if m is not None:
            return m
        runs = self.coord.runs
        return runs._model() if runs.model_status().get("available") else None

    @staticmethod
    def _model_is_local(model) -> bool:
        ep = str(getattr(getattr(model, "profile", None), "endpoint", "") or "")
        return ep.startswith("scripted://") or any(h in ep for h in ("127.0.0.1", "[::1]"))

    def _research_tags(self, ctx: dict) -> list[str]:
        """What a request may be researched with. A confidential one: the owner's own category names and nothing else (free words never leave). Otherwise its tags and those categories."""
        cats = [c for c in ctx["categories"] if c in self.profile.categories()]
        return cats if ctx["sensitivity"] == "confidential" else list(dict.fromkeys([*ctx["tags"], *cats]))

    def _research_blocked(self, ctx: dict) -> str:
        model = self._recommend_model()
        if model is None:
            return RC.REC_NO_MODEL
        if ctx["sensitivity"] == "confidential" and not self._model_is_local(model):
            return RC.REC_MODEL_CLOUD
        if not self._research_tags(ctx):
            return RC.REC_NO_TAGS
        return ""

    def _catalog_types(self) -> dict[str, str]:
        out: dict[str, str] = {}
        have = {t for e in self.envs.values() for t in e.get("task_types", [])}
        labels = json.loads((E.SKILLS_DIR / "router-environments" / "references" / "task_type_labels.json").read_text(encoding="utf-8")).get("labels", {})
        for f in sorted(DATA_ROOT.glob("*/suite.yaml")):
            d = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            if d.get("task_type") in have:
                out[d["task_type"]] = labels.get(d["task_type"]) or str(d.get("description", ""))[:60]
        return out

    def _known_snapshot(self, tags: list[str], rung1: Proposal) -> dict:
        tagset = set(tags)
        tagged = [{"id": k, "evidence_state": e.get("evidence_state"), "origin": e.get("origin"), "task_types": e.get("task_types", [])} for k, e in sorted(self.catalog.items()) if tagset & set(e.get("domains", []))]
        public = sum(1 for e in self.router.evidence.get("entries", []) if tagset & set(e.get("domains", [])))
        return {"tagged_entries": tagged, "leads": [x["id"] for x in rung1.leads], "public_evidence_entries": public, "verified_tagged_usable": any(o.tags for o in rung1.options)}

    def _start_research(self, ch, ctx: dict, rung1: Proposal) -> dict:
        model = self._recommend_model()
        rid = "rec-" + secrets.token_hex(3)
        tags = self._research_tags(ctx)
        researcher = Researcher(dir=Path(self.coord.state) / E.RESEARCHED_DIR_NAME, tags=tags, sensitivity=ctx["sensitivity"], task_text=ctx["text"], today=time.strftime("%Y-%m-%d", time.gmtime(self.coord.clock())),
                                fetch=getattr(self, "research_fetch", None) or real_fetch, log=lambda t, p: self.coord.log.add(t, p))
        slots = self.new_slots(level=ctx["sensitivity"], task_text=ctx["text"], local=lambda m=model: m)
        adapter = RecommendAdapter(owner_events=self.coord.runs._owner_event, tags=tags, sensitivity=ctx["sensitivity"], types=self._catalog_types(), known=self._known_snapshot(tags, rung1), researcher=researcher, slots=slots, task_text=ctx["text"])
        store = Store(self.coord.runs.dir / rid)
        target = A.new_target(store, TargetKind.recommend, "this-request")
        A.write_owner_event(store, "run_requested", {"kind": "recommend", "via": ch.name}, None)
        self._job(rid, "recommend", "researching", text=RC.REC_RESEARCHING.format(job=rid), sensitivity=ctx["sensitivity"])
        self.coord.log.add("recommend_started", {"run": rid, "channel": ch.name, "domains": tags, "level": ctx["sensitivity"], "tag": "GUARD"})
        self.recommend_runs[rid] = {"store": store, "adapter": adapter, "researcher": researcher, "slots": slots}
        asyncio.ensure_future(self._research_run(ch, ctx, rid, model, store, target, adapter))
        return self._out(True, RC.REC_RESEARCHING.format(job=rid), job_id=rid, researching=True)

    async def _research_run(self, ch, ctx: dict, rid: str, model, store, target, adapter: RecommendAdapter) -> None:
        job = self.jobs[rid]
        usage = None
        try:
            lib = library()
            loop = HarnessLoop(store, [adapter], model, config=RunConfig(max_steps=30, max_seconds=300.0, protocols=ProtocolSet.from_library(lib), library=lib))
            adapter.run_id = loop.run_id
            s = await loop.run(target)
            usage = {"calls": s.model_calls, "in": s.prompt_tokens, "out": s.completion_tokens}
            state = (s.protocol_history.get(target.id) or [{}])[-1].get("state") if s.protocol_history else None
            done = s.stop_reason == "protocol_done" and state in ("done", "none_fits")
            why = "" if done else f"stopped: {s.stop_reason}"
        except Exception as exc:  # noqa: BLE001 - the recommendation still goes ahead on known data
            done, state, why = False, None, f"{type(exc).__name__}: {str(exc)[:80]}"
        self.catalog, self.rejected_leads = E.load_catalog(extra_dir=Path(self.coord.state) / E.RESEARCHED_DIR_NAME)  # what research recorded is merged by the same loader and guard
        n = len(adapter.recorded)
        self.coord.log.add("recommend_finished", {"run": rid, "state": state or "stopped", "leads": n, "fetched": len(self.recommend_runs[rid]["researcher"].urls),
                                                  "loop_usage": usage, "slot_usage": self.recommend_runs[rid]["slots"].usage(), "research": self.recommend_runs[rid]["researcher"].summary(), "tag": "GUARD"})
        if adapter.decision is not None:
            d = adapter.decision
            self.coord.log.add("type_decision", {"run": rid, "picks": [{"where": w, "pick": v} for w, v in d.picks], "signals": d.signals, "resolved": d.resolved, "options": d.options, "escalated": d.escalated, "tag": "GUARD"})
        if done and state == "none_fits":
            p = self.build_proposal(ch, domains=ctx["tags"], sensitivity=ctx["sensitivity"])
            body = RC.REC_NONE_FITS + (RC.REC_NONE_FITS_WHY.format(why=adapter.type_why) if adapter.type_why else "") + ("\n" + self.recommendation_text(Proposal(p.task_type, p.domains, p.sensitivity, True, [], p.leads, [], None, p.hash)) if p.leads else "")
            job.state, job.text = "refused", body
            self.coord.log.add("recommendation_refused", {"run": rid, "refusal": "no_type_fits", "tag": "GUARD"})
            return
        ctx = {**ctx, "note": (RC.REC_RESEARCH_DONE.format(n=n) + (RC.REC_DECLINED.format(why=adapter.declined) if adapter.declined else "") if done else RC.REC_RESEARCH_STOPPED.format(why=why))}
        if done and adapter.pages_read == 0 and adapter.fetch_failures:
            ctx["note"] = RC.REC_RESEARCH_UNREADABLE.format(n=adapter.fetch_failures)
        if done and adapter.chosen and adapter.chosen != NONE_FITS:
            ctx["task_type"], ctx["type_why"] = adapter.chosen, adapter.type_why
        if done and adapter.type_options:  # no two independent answers agree on the kind of work: ONE question to the owner before the recommendation (code never guesses)
            self._ask_type(ch, ctx, rid, adapter.type_options)
            return
        self._finish_presentation(ch, ctx, rid)

    def _finish_presentation(self, ch, ctx: dict, rid: str) -> str:
        job = self.jobs[rid]
        out = self._present(ch, ctx)
        job.state, job.text, job.intent_id = ("awaiting_answer" if out.get("question") else "refused"), out["text"], out.get("intent_id")
        if out.get("intent_id"):
            self.intent_jobs[out["intent_id"]] = rid
        job.data["plan_hash"] = out.get("plan_hash")
        return out["text"]

    def _ask_type(self, ch, ctx: dict, rid: str, options: list[str]) -> None:
        job, types = self.jobs[rid], self._catalog_types()
        opts = [{"id": o, "label": RC.REC_TYPE_OPTION.format(type=o, label=types.get(o, ""))} for o in options if o in types]
        if NONE_FITS in options or not opts:
            opts.append({"id": NONE_FITS, "label": RC.REC_TYPE_NONE})
        i = self.coord.harness.ask_chat(None, key=f"recommend-type:{rid}", text=RC.REC_TYPE_ASK.format(picks=", ".join(options)), options=opts, sensitivity="internal", on_choice=lambda o, c=ch, x=ctx, r=rid: self._on_type(c, x, r, o))
        job.state, job.text, job.intent_id = "awaiting_type", i.text_fallback, i.id
        self.intent_jobs[i.id] = rid
        self.coord.log.add("type_asked", {"run": rid, "options": [o["id"] for o in opts], "tag": "GUARD"})

    def _on_type(self, ch, ctx: dict, rid: str, option: str) -> str:
        self.coord.log.add("type_answered", {"run": rid, "option": option, "gesture": "chat_choice", "relayed_by_model": ch.model_in_middle, "tag": "OWNER"}, owner=True)
        if option == NONE_FITS:
            self.jobs[rid].state, self.jobs[rid].text = "refused", RC.REC_NONE_FITS
            self.coord.log.add("recommendation_refused", {"run": rid, "refusal": "no_type_fits", "tag": "GUARD"})
            return RC.REC_NONE_FITS
        ctx = {**ctx, "task_type": option, "type_why": RC.REC_TYPE_DECIDED.format(type=option)}
        return RC.REC_TYPE_DECIDED.format(type=option) + "\n" + self._finish_presentation(ch, ctx, rid)

    # ---- the question and its answer -------------------------------------------------------------------------------------------------------------

    def _present(self, ch, ctx: dict) -> dict:
        tt = ctx.get("task_type")
        p = self.build_proposal(ch, domains=ctx["tags"], sensitivity=ctx["sensitivity"], task_type=tt, unclassified=tt is None, type_why=ctx.get("type_why", ""))
        body = self.recommendation_text(p) + (("\n" + ctx["note"]) if ctx.get("note") else "")
        if not p.options:
            self.coord.log.add("recommendation_refused", {"hash": p.hash, "refusal": p.refusal.code if p.refusal else "none", "tag": "GUARD"})
            return self._out(False, body, refusal=p.refusal.code if p.refusal else "nothing_to_propose", plan_hash=p.hash)
        opts = []
        for o in p.options:
            label = (RC.REC_USE_DEFAULT if o.id == "use_default" else RC.REC_USE_RUNNER if o.id == "use_runner" else RC.REC_USE_CLEAR.format(env=o.env))
            opts.append({"id": o.id, "label": label, "detail": self._key_text(o.route_key)})
        opts.append({"id": "neither", "label": RC.REC_NEITHER})
        i = self.coord.harness.ask_chat(None, key=f"recommend:{p.hash}", text=body, options=opts, sensitivity="internal",
                                        on_choice=lambda o, c=ch, x=ctx, h=p.hash: self._on_recommendation(c, x, h, o))
        self.coord.log.add("recommendation_made", {"hash": p.hash, "default": p.options[0].route_key, "options": [o.id for o in p.options], "leads": [x["id"] for x in p.leads], "tag": "GUARD"})
        return self._out(True, i.text_fallback, intent_id=i.id, question=True, plan_hash=p.hash)

    def _on_recommendation(self, ch, ctx: dict, h: str, option: str) -> str:
        if option == "neither":
            self.coord.log.add("recommendation_answered", {"hash": h, "option": "neither", "gesture": "chat_choice", "tag": "OWNER"}, owner=True)
            return RC.REC_NEITHER_DONE
        p = self.build_proposal(ch, domains=ctx["tags"], sensitivity=ctx["sensitivity"], task_type=ctx.get("task_type"), unclassified=ctx.get("task_type") is None, type_why=ctx.get("type_why", ""))
        if p.hash != h:  # the plan the owner answered is not the plan that holds now: his answer is for the old one and is not used
            return RC.REC_STALE.format(text=self._present(ch, ctx)["text"])
        chosen = next((o for o in p.options if o.id == option), None)
        if chosen is None:
            return RC.REC_STALE.format(text=self._present(ch, ctx)["text"])
        self.coord.log.add("recommendation_answered", {"hash": h, "option": option, "route": chosen.route_key, "gesture": "chat_choice", "relayed_by_model": ch.model_in_middle, "tag": "OWNER"}, owner=True)
        if chosen.kind == "clear_and_use":
            self.on_clearance(chosen.env, "clear")
        out = self._create(ch, text=ctx["text"], task_type=chosen.task_type, sensitivity=ctx["sensitivity"], check=ctx["check"], domain=ctx["tags"], only_env=chosen.env)
        return RC.REC_DECISION.format(choice=self._key_text(chosen.route_key), hash=h) + "\n" + out["text"]
    def _on_recommendation(self, ch, ctx: dict, h: str, option: str) -> str:
        if option == "neither":
            self.coord.log.add("recommendation_answered", {"hash": h, "option": "neither", "gesture": "chat_choice", "tag": "OWNER"}, owner=True)
            return RC.REC_NEITHER_DONE
        p = self.build_proposal(ch, domains=ctx["tags"], sensitivity=ctx["sensitivity"], task_type=ctx.get("task_type"), unclassified=ctx.get("task_type") is None, type_why=ctx.get("type_why", ""))
        if p.hash != h:  # the plan the owner answered is not the plan that holds now: his answer is for the old one and is not used
            return RC.REC_STALE.format(text=self._present(ch, ctx)["text"])
        chosen = next((o for o in p.options if o.id == option), None)
        if chosen is None:
            return RC.REC_STALE.format(text=self._present(ch, ctx)["text"])
        self.coord.log.add("recommendation_answered", {"hash": h, "option": option, "route": chosen.route_key, "gesture": "chat_choice", "relayed_by_model": ch.model_in_middle, "tag": "OWNER"}, owner=True)
        if chosen.kind == "clear_and_use":
            self.on_clearance(chosen.env, "clear")
        out = self._create(ch, text=ctx["text"], task_type=chosen.task_type, sensitivity=ctx["sensitivity"], check=ctx["check"], domain=ctx["tags"], only_env=chosen.env)
        return RC.REC_DECISION.format(choice=self._key_text(chosen.route_key), hash=h) + "\n" + out["text"]

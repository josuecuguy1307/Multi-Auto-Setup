"""Chains (skill `harness-chains`): the harness carries a request through the steps it needs, by itself, from DATA (`chains.json`, `input_kinds.json`). The chat model relays each question and each answer; it is never
asked to remember a step. A step is a method that returns an outcome now, or arranges to be resumed later (a question answered, a node task finished, a setup run ended) and returns None. The transitions are the
data's: this module knows no flow, no folder and no machine.

Actors: a transition is GUARD; the owner's answers are OWNER events (chat_choice); the router's choice stays the router's. Nothing here approves for the owner."""

from __future__ import annotations

import asyncio
import hashlib
import time
import json
import secrets
from typing import Any

from envrec_coordinator import chain_strings as CS
from envrec_coordinator import options as O
from envrec_coordinator.draft import facts_of
from envrec_coordinator.recommend import generic_type
from envrec_coordinator.router import Refusal, RouteDecision
from envrec_coordinator.router.catalog import knows_task_type
from envrec_coordinator.router.model import EntryChat, Exclude, Task
from envrec_net import envrun as E
from envrec_os import SKILLS_DIR

CHAIN_DIR = SKILLS_DIR / "harness-chains" / "references"
JOB_DONE = ("done", "aborted", "refused", "declined", "failed", "exhausted", "route_changed", "unchecked", "done_unchecked")


def load_chains() -> dict:
    return json.loads((CHAIN_DIR / "chains.json").read_text(encoding="utf-8"))["chains"]


def load_kinds() -> dict:
    return json.loads((CHAIN_DIR / "input_kinds.json").read_text(encoding="utf-8"))["kinds"]


def _digest(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


class ChainTools:
    """Mixed into `Services`."""

    # ---- starting ---------------------------------------------------------------------------------------------------------------------------------

    def input_specs(self) -> dict:
        return load_kinds()

    def chain_task(self, ch, *, text: str, task_type: str, sensitivity: str, check, domain, categories, inputs: list) -> dict:
        kinds = load_kinds()
        items = []
        for raw in inputs if isinstance(inputs, list) else []:
            kind = raw.get("kind") if isinstance(raw, dict) else None
            if kind not in kinds:
                return self._out(False, CS.UNKNOWN_KIND.format(kind=kind, kinds=", ".join(sorted(kinds))))
            spec = kinds[kind]
            arg = raw.get(spec["params"]["folder"]) if "folder" in spec["params"] else None
            missing = [a for a in spec["args"] if a != "machine" and not str(raw.get(a, "")).strip()]
            if missing or not str(raw.get("machine", "")).strip():
                return self._out(False, CS.BAD_INPUT.format(args=", ".join(a for a in spec["args"] if a != "machine"), kinds=", ".join(sorted(kinds))))
            try:
                node = self.coord.harness._resolve_node(str(raw["machine"]))
            except Exception:  # noqa: BLE001 - an unknown or ambiguous name is answered below
                node = None
            if node is None:
                names = ", ".join(self._name(r.node_id) for r in self.coord.registry.all()) or "none"
                return self._out(False, CS.NEEDS_MACHINE.format(kind=kind, names=names))
            if node not in self.coord._sessions:
                return self._out(False, CS.NOT_CONNECTED.format(machine=self._name(node)))
            items.append({"kind": kind, "node": node, "machine": self._name(node), "args": {a: str(raw[a]).strip()[:400] for a in spec["args"] if a != "machine"}, "arg": arg and str(arg).strip()})
        if not items:
            return self._out(False, CS.BAD_INPUT.format(args="folder", kinds=", ".join(sorted(kinds))))
        chain_id = "c-" + secrets.token_hex(3)
        chain = {"id": chain_id, "flow": "task_with_inputs", "step": None, "visits": {}, "ch": ch, "ctx": {"text": text, "task_type": task_type, "level": sensitivity, "check": check, "domain": domain, "categories": categories or [],
                                                                                                              "inputs": items, "held": [], "machine": items[0]["node"]}}
        self.chains[chain_id] = chain
        self._job(chain_id, "chain", "starting", sensitivity=sensitivity)
        self.coord.log.add("chain_started", {"chain": chain_id, "flow": chain["flow"], "inputs": [i["kind"] for i in items], "level": sensitivity, "channel": ch.name, "tag": "GUARD"})
        self._advance(chain, None)
        j = self.jobs[chain_id]
        return self._out(True, j.text, job_id=chain_id, intent_id=j.intent_id, question=bool(j.intent_id), chain=chain_id)

    # ---- the engine --------------------------------------------------------------------------------------------------------------------------------

    def _say(self, chain: dict, state: str, text: str, intent: str | None = None, hint: bool = True) -> None:
        job = self.jobs[chain["id"]]
        if intent:
            self.intent_jobs[intent] = chain["id"]  # the answer to this question belongs to this chain: it can say so
        job.state, job.text, job.intent_id = state, text + (f"\n(intent_id for mas_answer: {intent})" if intent and hint and f"intent_id for mas_answer: {intent}" not in text else ""), intent

    def _advance(self, chain: dict, outcome: str | None) -> None:
        flow = load_chains()[chain["flow"]]
        step = flow["start"] if outcome is None else flow["steps"][chain["step"]]["on"].get(outcome, "end")
        if outcome is not None:
            self.coord.log.add("chain_step", {"chain": chain["id"], "step": chain["step"], "outcome": outcome, "next": step, "tag": "GUARD"})
        while step != "end":
            chain["visits"][step] = chain["visits"].get(step, 0) + 1
            if chain["visits"][step] > int(flow.get("max_visits", {}).get(step, 3)):
                self._say(chain, "refused", CS.TOO_MANY.format(step=step))
                return
            chain["step"] = step
            try:
                out = getattr(self, "_step_" + flow["steps"][step]["do"])(chain)
            except Exception as exc:  # noqa: BLE001 - a chain that cannot go on says so; it never takes the coordinator down
                self._say(chain, "failed", CS.CHAIN_ERROR.format(why=type(exc).__name__))
                return
            if out is None:
                return  # waiting: a question, a node task or a run will resume it
            self.coord.log.add("chain_step", {"chain": chain["id"], "step": step, "outcome": out, "tag": "GUARD"})
            step = flow["steps"][step]["on"].get(out, "end")

    def _resume(self, chain: dict, outcome: str) -> str:
        self._advance(chain, outcome)
        return self.jobs[chain["id"]].text

    # ---- steps ---------------------------------------------------------------------------------------------------------------------------------------

    def _step_approve_inputs(self, chain: dict) -> str | None:
        c, kinds = chain["ctx"], load_kinds()
        item = next((x for x in c["inputs"] if "approved" not in x), None)
        if item is None:
            return "done"
        spec = kinds[item["kind"]]
        fmt = {**item["args"], "machine": item["machine"]}
        opts = [{"id": "approve", "label": spec["yes"]}, {"id": "decline", "label": spec["no"]}]
        i = self.coord.harness.ask_chat(None, key=f"chain:{chain['id']}:in{c['inputs'].index(item)}", text=spec["question"].format(**fmt), options=opts, sensitivity="internal",  # the question holds a folder name and what is read, never the listing
                                        on_choice=lambda o, ch_=chain, it=item: self._on_input(ch_, it, o))
        self._say(chain, "awaiting_answer", i.text_fallback, i.id)
        return None

    def _on_input(self, chain: dict, item: dict, option: str) -> str:
        item["approved"] = option == "approve"
        spec = load_kinds()[item["kind"]]
        self.coord.log.add("input_answered", {"chain": chain["id"], "kind": item["kind"], "machine": item["machine"], "option": option, "gesture": "chat_choice", "tag": "OWNER"}, owner=True)
        if option != "approve":
            self._say(chain, "declined", CS.DECLINED.format(**{**item["args"], "machine": item["machine"]}))
            return self.jobs[chain["id"]].text
        more = any("approved" not in x for x in chain["ctx"]["inputs"])
        self._resume(chain, "more" if more else "done")
        return self.jobs[chain["id"]].text

    def _step_read_inputs(self, chain: dict) -> str | None:
        c, kinds = chain["ctx"], load_kinds()
        for item in c["inputs"]:
            spec = kinds[item["kind"]]
            item["rec"] = self.coord.tasks.start(item["node"], spec["node_task"], {p: item["args"][a] for p, a in spec["params"].items()}, by="GUARD")
        first = c["inputs"][0]
        self._say(chain, "reading", CS.LISTING_STARTED.format(what=first["args"].get("folder", first["kind"]), machine=first["machine"], job=chain["id"]))
        asyncio.ensure_future(self._await_inputs(chain))
        return None

    async def _await_inputs(self, chain: dict) -> None:
        c, kinds = chain["ctx"], load_kinds()
        try:
            for item in c["inputs"]:
                try:
                    await asyncio.wait_for(item["rec"].done.wait(), 60)
                except TimeoutError:
                    pass
            for item in c["inputs"]:
                rec, spec = item["rec"], kinds[item["kind"]]
                if rec.status != "done":
                    from envrec_os import folders as _folders
                    why = CS.LISTING_REASONS.get(rec.reason or "", rec.reason or rec.status).format(names=", ".join(sorted(_folders.load_table()["folders"])))  # the well-known names are the os-folders data's
                    self._say(chain, "failed", CS.LISTING_FAILED.format(what=item["args"].get("folder", item["kind"]), machine=item["machine"], why=why))
                    return
                hid = "held-" + secrets.token_hex(4)
                res = rec.result
                rows = res.get(spec["rows"], [])
                lines = [" | ".join(str(r.get(col, "")) for col in spec["columns"]) for r in rows]
                header = spec["payload_header"].format(**{**item["args"], "machine": item["machine"]})
                names = sorted({str(r.get(spec["name_field"], "")) for r in rows if len(str(r.get(spec["name_field"], ""))) >= 3}, key=len, reverse=True)
                text = header + "\n" + "\n".join(lines) + (f"\n(and {res['cut']} more entries not shown)" if res.get("cut") else "")
                self.held[hid] = {"text": text, "names": names, "summary": spec["summary"].format(**{**item["args"], "machine": item["machine"], "count": res.get("count", len(rows)), "cut": res.get("cut", 0), "total_bytes": res.get("total_bytes", 0)}), "answer_travels": bool(spec.get("answer_travels"))}
                c["held"].append(hid)
                self.coord.log.add("input_read", {"chain": chain["id"], "kind": item["kind"], "machine": item["machine"], "entries": len(rows), "held": hid, "tag": "GUARD"})  # counts only: no name is ever logged
            self._resume(chain, "done")
        except Exception as exc:  # noqa: BLE001
            self._say(chain, "failed", CS.CHAIN_ERROR.format(why=type(exc).__name__))

    def _payload(self, chain: dict) -> str:
        c = chain["ctx"]
        return c["text"] + "\n\n" + "\n\n".join(self.held[h]["text"] for h in c["held"])

    def _chain_task(self, chain: dict, tid: str) -> Task:
        c = chain["ctx"]
        tt = c["task_type"] if knows_task_type(self.envs, c["task_type"]) else generic_type()
        others = [r.node_id for r in self.coord.registry.all() if r.node_id != c["machine"]]  # "a model on that computer": every other computer is out, as a hard filter
        return Task(task_id=tid, task_type=tt, sensitivity=c["level"], entry_chat=self._chat(chain["ch"]), created_by="OWNER", payload_ref="chain", held_inputs=list(c["held"]), domain=c["domain"] or [],
                    exclude=Exclude(machine_ids=others[:64]) if others else None)

    def _step_route(self, chain: dict) -> str | None:
        res = self.router.explain(self._chain_task(chain, "chain-" + chain["id"]))
        chain["ctx"]["route"] = res
        if isinstance(res, RouteDecision):
            self._say(chain, "routing", CS.ROUTED_TO.format(route=self._key_text(res.route.key)))
            return "routed"
        self._say(chain, "refused", CS.NO_ROUTE.format(reason=res.reason))
        return res.code if res.code in ("no_local_candidate", "no_candidate", "provisional_only") else "refused"

    def _step_propose_setup(self, chain: dict) -> str | None:
        c = chain["ctx"]
        node, machine = c["machine"], self._name(c["machine"])
        analysis = self.coord.runs.stored_analysis(node)
        flow = load_chains()[chain["flow"]]
        stale = analysis is not None and time.time() - float(analysis.get("stored_at", 0)) > float(flow.get("analysis_max_age_s", 0) or 10 ** 12)
        if analysis is None or stale:
            if chain["visits"].get("analyze", 0) >= int(flow.get("max_visits", {}).get("analyze", 1)):
                self._say(chain, "refused", CS.SETUP_NO_ANALYSIS.format(machine=machine))  # it was read in this chain already and still is not usable
                return "unavailable"
            return "no_analysis"
        res = O.build_options(facts_of(analysis), choice="local", sensitivity=c["level"], prefer_publisher=self.profile.get("preferred_publisher"))
        opts = [o for o in res["options"] if o.get("kind") == "local" and (o.get("fit") or {}).get("fits")][:2]
        if not opts:
            why = "; ".join(x["reason"] for x in res["removed"][:2]) or "nothing fits"
            self._say(chain, "refused", CS.SETUP_NO_FIT.format(machine=machine, why=why))
            return "unavailable"
        h = _digest(node, c["level"], [o["id"] for o in opts])[:12]
        gib = lambda b: f"{b / 2 ** 30:.1f} GiB" if b else "size not stated"  # noqa: E731

        def facts(o):
            f = o["fit"]
            return CS.SETUP_FACTS.format(status=o["status"], size=gib(o.get("model_bytes")), need=f["needed_gib"], budget=f["budget_gib"], licence=o.get("license_name") or o.get("license"))
        lines = [CS.SETUP_HEAD.format(machine=machine, level=c["level"]), CS.SETUP_DEFAULT.format(title=opts[0]["title"], facts=facts(opts[0]))]
        options = [{"id": "use_default", "label": CS.SETUP_USE_DEFAULT, "detail": opts[0]["title"]}]
        if len(opts) > 1:
            lines.append(CS.SETUP_RUNNER.format(title=opts[1]["title"], facts=facts(opts[1])))
            options.append({"id": "use_runner", "label": CS.SETUP_USE_RUNNER, "detail": opts[1]["title"]})
        options.append({"id": "neither", "label": CS.SETUP_NEITHER})
        lines.append(CS.SETUP_ASK.format(hash=h))
        c["setup_options"] = {"use_default": opts[0], "use_runner": opts[1] if len(opts) > 1 else None}
        i = self.coord.harness.ask_chat(None, key=f"chain:{chain['id']}:setup:{h}", text="\n".join(lines), options=options, sensitivity="internal", on_choice=lambda o, ch_=chain, hh=h: self._on_setup(ch_, hh, o))
        self._say(chain, "awaiting_answer", i.text_fallback, i.id)
        return None

    def _on_setup(self, chain: dict, h: str, option: str) -> str:
        c = chain["ctx"]
        picked = c["setup_options"].get(option)
        self.coord.log.add("setup_option_picked", {"chain": chain["id"], "node": c["machine"], "option": picked["id"] if picked else "neither", "plan": h, "gesture": "chat_choice", "tag": "OWNER"}, owner=True)
        if picked is None:
            self._say(chain, "declined", CS.SETUP_DECLINED)
            return self.jobs[chain["id"]].text
        self.coord.runs.record_pick_option(c["machine"], picked, chain["id"])
        self._resume(chain, "chosen")
        return CS.SETUP_PICKED.format(title=picked["title"], hash=h) + "\n" + self.jobs[chain["id"]].text

    def _step_analyze(self, chain: dict) -> str | None:
        """Read the computer (the analysis protocol, read-only) because no usable analysis is stored. Started by the harness: not the owner's press, no approval is asked for a read."""
        c = chain["ctx"]
        run = self.coord.runs.start_analysis(c["machine"], "harness", model=getattr(self, "analysis_model", None))
        if run.task is None:
            self._say(chain, "failed", CS.ANALYZE_FAILED.format(machine=self._name(c["machine"]), why=run.reason or run.status))
            return "failed"
        c["analysis_run"] = run.id
        self._say(chain, "analyzing", CS.ANALYZE_STARTED.format(machine=self._name(c["machine"]), run=run.id))
        run.task.add_done_callback(lambda _f, ch_=chain, r=run: self._analysis_ended(ch_, r))
        return None

    def _analysis_ended(self, chain: dict, run) -> None:
        if run.status == "done" and self.coord.runs.stored_analysis(chain["ctx"]["machine"]) is not None:
            self._resume(chain, "done")
        else:
            why = (run.reason or run.status) if run.status != "done" else "it ended without an analysis the harness can use"
            self._say(chain, "failed", CS.ANALYZE_FAILED.format(machine=self._name(chain["ctx"]["machine"]), why=why))

    def _step_install(self, chain: dict) -> str | None:
        c = chain["ctx"]
        run = self.coord.runs.start_install(c["machine"], "chat", model=getattr(self, "install_model", None))
        if run.task is None:
            self._say(chain, "failed", CS.SETUP_RUN_FAILED.format(why=run.reason or run.status))
            return "failed"
        c["install_run"] = run.id
        self._say(chain, "installing", CS.INSTALL_STARTED.format(run=run.id, machine=self._name(c["machine"])))
        run.task.add_done_callback(lambda _f, ch_=chain, r=run: self._install_ended(ch_, r))
        return None

    def _install_ended(self, chain: dict, run) -> None:
        if run.status == "done":
            self._resume(chain, "done")
        else:
            self._say(chain, "failed", CS.SETUP_RUN_FAILED.format(why=run.reason or run.status))

    def _step_measure(self, chain: dict) -> str | None:
        c = chain["ctx"]
        res = c["route"]
        rank = {name: i for i, name in enumerate(self.policy.cost_order)}
        keys = sorted((k for k, why in (res.details.get("dropped", {}) if isinstance(res, Refusal) else {}).items() if "measured" in why), key=lambda k: (rank.get(self._cost_of(k), 9), k))
        if not keys:
            self._say(chain, "refused", CS.MEASURE_NONE.format(why=res.reason if isinstance(res, Refusal) else "no combination is waiting for a measurement"))
            return "failed"
        env, _model, access, mid = keys[0].split("|")
        tt = self._chain_task(chain, "x").task_type
        out = self.run_admission(chain["ch"], environment=env, task_type=tt, machine=mid, access=access)
        if not out.get("ok"):
            self._say(chain, "failed", out["text"])
            return "failed"
        self._say(chain, "awaiting_approval", CS.MEASURE_STARTED.format(level=c["level"], intent=out.get("intent_id"), job=out.get("job_id")) + "\n" + out["text"], out.get("intent_id"), hint=False)
        asyncio.ensure_future(self._watch_job(chain, out["job_id"]))
        return None

    def _cost_of(self, key: str) -> str:
        env, _m, access, _mid = key.split("|")
        return next((a["cost_class"] for a in self.envs.get(env, {}).get("accesses", []) if a["id"] == access), "subscription")

    async def _watch_job(self, chain: dict, job_id: str) -> None:
        for _ in range(7200):
            job = self.jobs.get(job_id)
            if job is not None and job.state in JOB_DONE:
                break
            await asyncio.sleep(0.5)
        job = self.jobs.get(job_id)
        if job is not None and job.state == "done":
            self._resume(chain, "done")
        else:
            self._say(chain, "failed", CS.MEASURE_FAILED.format(state=job.state if job else "gone", level=chain["ctx"]["level"]))

    def _step_create_task(self, chain: dict) -> str | None:
        c = chain["ctx"]
        tt = self._chain_task(chain, "x").task_type
        held = {"ids": list(c["held"]), "chat_text": c["text"], "machine": c["machine"]}
        out = self._create(chain["ch"], text=self._payload(chain), task_type=tt, sensitivity=c["level"], check=c["check"], domain=c["domain"], categories=c["categories"], held=held)
        self._say(chain, "task_created" if out.get("ok") else "refused", CS.TASK_NEXT.format(text=out["text"]) if out.get("ok") else out["text"], out.get("intent_id"))
        self.jobs[chain["id"]].data["task_id"] = out.get("task_id")
        return "created"

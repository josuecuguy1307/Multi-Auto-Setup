"""Measure the recommender with the cloud model reached through its node (a services tool, mixed into `Services`): the owner approves ONE chat question that shows what will be spent; then the harness runs the
same measurement as the local one (valid-lead rate and task-type accuracy, with and without the harness) with `NodeModel` as the model, and keeps the numbers. It is bounded by a call cap in the data, it needs the
owner's recorded answer on cloud use, and nothing in it is confidential: the gold tasks and the domain words are fixed public fixtures."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path

from envrec_coordinator import measure as M
from envrec_coordinator.node_model import NodeModel
from envrec_coordinator.research import real_fetch
from envrec_coordinator.slots import load_slots

FIX = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "recommender"
MS_ASK = ("Measure the recommender with the cloud model ({env}) through {machine}? It asks that model the 12 fixed gold tasks (plain and with the harness) and runs the research on 6 fixed domains (plain and with the harness): "
          "about {calls} calls at most (the cap is {cap}), reading public GitHub pages from this computer. Your recorded answer on cloud use allows it. Plan {hash}. {answer}")
MS_NO_FIXTURE = "The fixed gold set and domain list are not in this installation (tests/fixtures/recommender), so there is nothing to measure."
MS_NOT_ALLOWED = "Your recorded answer on cloud use does not allow asking the cloud, so nothing was measured."
MS_NO_ROUTE = "No paired computer that is online has the cloud access, so nothing was measured (the key stays on that computer)."
MS_DONE = "Measured with {env} ({returned}): task type right {r}/{n} plain, {rh}/{n} with the harness (asked you {ah}); valid leads {lp}/{lpa} plain, {lh}/{lha} with the harness; {calls} calls, {tin} tokens in, {tout} out. Saved: {file}."


class MeasureTools:
    def measure_recommender(self, ch, *, machine: str | None = None) -> dict:
        data = load_slots()
        if not (FIX / "gold_task_types.json").is_file():
            return self._out(False, MS_NO_FIXTURE)
        answer, _ = self.coord.cloud_use_answer()
        if answer is None or answer in data["authority"]["refused_values"]:
            return self._out(False, MS_NOT_ALLOWED)
        route = self._judge_route(data)
        if route is None:
            return self._out(False, MS_NO_ROUTE)
        cap = int(data["measurement"]["max_calls"])
        gold = (FIX / "gold_task_types.json").read_bytes()
        doms = (FIX / "measure_domains.json").read_bytes()
        plan_hash = "sha256:" + hashlib.sha256(gold + doms + data["escalation"]["environment_id"].encode() + str(cap).encode()).hexdigest()
        estimate = 12 + 36 + 6 * 9
        conf = self.coord.harness.request_confirmation(None, summary="Measure the recommender with the cloud model", plan_hash=plan_hash, command=f"at most {cap} calls to {data['escalation']['environment_id']} through {self._name(route.machine_id)}",
                                                       undo="nothing to undo: it only records numbers", sensitivity="public", kind="measurement")
        jid = "measure-" + plan_hash[7:15]
        self._job(jid, "measurement", "awaiting_approval", intent_id=conf.intent.id, sensitivity="public", data={"cap": cap})
        conf.future.add_done_callback(lambda f, j=jid: self._measure_decided(j, f.result()))
        return self._out(True, MS_ASK.format(env=data["escalation"]["environment_id"], machine=self._name(route.machine_id), calls=estimate, cap=cap, hash=plan_hash[7:19], answer=self.coord.harness.answer_how(conf.intent.id)),
                         job_id=jid, intent_id=conf.intent.id)

    def _measure_decided(self, jid: str, ok: bool) -> None:
        job = self.jobs[jid]
        if not ok:
            job.state, job.text = "declined", "The owner did not approve it, so nothing was asked of the cloud."
            return
        job.state = "running"
        asyncio.ensure_future(self._measure_run(job))

    async def _measure_run(self, job) -> None:
        data = load_slots()
        gold = json.loads((FIX / "gold_task_types.json").read_text())["tasks"]
        doms = json.loads((FIX / "measure_domains.json").read_text())["domains"]
        model = NodeModel(self, level="public", label="measure")
        cap = int(data["measurement"]["max_calls"])
        real_complete = model.complete

        async def capped(system, user):
            if model.n >= cap:
                from envrec.model.base import ModelError
                raise ModelError(f"the cap of {cap} cloud calls for one measurement is reached")
            return await real_complete(system, user)
        model.complete = capped  # type: ignore[method-assign]
        try:
            out = {}
            fetch = getattr(self, "research_fetch", None) or real_fetch
            out.update(await M.run_all(model, doms, gold, only="types", fetch=fetch))
            out.update(await M.run_all(model, doms, gold, only="leads", fetch=fetch))
        except Exception as exc:  # noqa: BLE001 - reported as the job's failure, never fatal
            job.state, job.text = "failed", f"The measurement stopped: {type(exc).__name__}: {str(exc)[:160]}"
            return
        calls = model.n
        tin = sum(v.get("tokens_in", 0) for k in ("types_harness", "types_plain") for v in [out[k]]) + sum(r.get("tokens_in", 0) for k in ("leads_harness", "leads_plain") for r in out[k]["rows"])
        tout = sum(v.get("tokens_out", 0) for k in ("types_harness", "types_plain") for v in [out[k]]) + sum(r.get("tokens_out", 0) for k in ("leads_harness", "leads_plain") for r in out[k]["rows"])
        returned = sorted({m for k in ("types_harness", "types_plain") for m in out[k].get("models_returned", {})})
        doc = {"measured_at": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(self.coord.clock())) + "Z", "environment": data["escalation"]["environment_id"], "models_returned_by_the_api": returned, "cloud_calls": calls,
               "tokens_in": tin, "tokens_out": tout, "labels": {"fetches": "REAL (the research source's public API, from this computer)", "cloud model": "REAL (through the node that holds the key)"}, **{"cloud_" + k: v for k, v in out.items()}}
        d = Path(self.coord.state) / "measurements"
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"recommender-cloud-{int(self.coord.clock())}.json"
        f.write_text(json.dumps(doc, indent=1), encoding="utf-8")
        t, th, lp, lh = out["types_plain"], out["types_harness"], out["leads_plain"]["summary"], out["leads_harness"]["summary"]
        job.state = "done"
        job.text = MS_DONE.format(env=data["escalation"]["environment_id"], returned=", ".join(returned) or "model id not reported", r=t["right"], n=t["tasks"], rh=th["right"], ah=th["asked_owner"], lp=lp["valid"], lpa=lp["attempted"], lh=lh["valid"], lha=lh["attempted"],
                                  calls=calls, tin=tin, tout=tout, file=f.name)
        job.data.update(file=str(f))
        self.coord.log.add("recommender_measured", {"calls": calls, "file": f.name, "tag": "GUARD"})

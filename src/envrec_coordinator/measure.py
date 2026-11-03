"""Measurement of the recommender, by code, with denominators (used by tests/verify_recommender_v2.py and by the coordinator's measurement tool). Two things are measured for ANY model adapter, with the harness
(the research loop with its guards; the independent picks with their signals) and without it (one plain prompt to the same model), both scored by the same code:

  valid-lead rate    = leads that pass license, active, not archived, enough stars and the domain tags in the README  /  leads the model tried to record, over a fixed list of domains
  task-type accuracy = picks equal to the gold type  /  tasks of the fixed gold set (the harness may instead ask the owner: counted apart, never as right or wrong)

The model is whatever adapter is handed in: the local harness model, or a model reached through a node (`node_model.NodeModel`). Nothing here names a model."""

from __future__ import annotations

import json
import tempfile
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from envrec.contracts import TargetKind
from envrec.loop import RunConfig
from envrec.protocols import ProtocolSet
from envrec.skills.discover import library
from envrec.store import Store
from envrec_coordinator import agent as A
from envrec_coordinator.recommend_agent import RecommendAdapter
from envrec_coordinator.research import Refused, Researcher, real_fetch
from envrec_coordinator.runs import HarnessLoop
from envrec_coordinator.slots import Slots, SlotRefused, decide, load_slots
from envrec_net import envrun as E

LABELS = json.loads((E.SKILLS_DIR / "router-environments" / "references" / "task_type_labels.json").read_text(encoding="utf-8"))["labels"]


async def leads_with_harness(model, dom: dict, tmp: Path, *, fetch: Callable = real_fetch) -> dict:
    tags, today = dom["tags"], time.strftime("%Y-%m-%d", time.gmtime())
    r = Researcher(dir=tmp / dom["id"] / "researched", tags=tags, sensitivity="public", fetch=fetch, today=today)
    adapter = RecommendAdapter(owner_events=lambda *a, **k: None, tags=tags, sensitivity="public", types=dict(LABELS), known={"tagged_entries": [], "leads": [], "public_evidence_entries": 0, "verified_tagged_usable": False}, researcher=r)
    store = Store(tmp / dom["id"] / "run")
    target = A.new_target(store, TargetKind.recommend, "this-request")
    lib = library()
    loop = HarnessLoop(store, [adapter], model, config=RunConfig(max_steps=30, max_seconds=420.0, protocols=ProtocolSet.from_library(lib), library=lib))
    adapter.run_id = loop.run_id
    s = await loop.run(target)
    refusals: Counter = Counter()
    for e in store.events():
        if e.type == "tool_result" and e.payload.get("ok") is False:
            err = str(e.payload.get("error", ""))
            refusals[err.split(":")[1].strip() if ":" in err else "other"] += 1
    sm = r.summary()
    return {"domain": dom["id"], "stop": s.stop_reason, "model_calls": s.model_calls, "tokens_in": s.prompt_tokens, "tokens_out": s.completion_tokens, "searches": sm["searches"], "requests": sm["requests"],
            "attempted": sm["attempted"], "valid": sm["recorded"], "rejected": sm["rejected"], "tool_refusals": dict(refusals)}


async def leads_plain(model, dom: dict, tmp: Path, *, fetch: Callable = real_fetch) -> dict:
    """One plain prompt: the same model sees the first page of a plain search for the tag (no qualifiers, no guards) and names up to three items; the same code scores them."""
    tags, today = dom["tags"], time.strftime("%Y-%m-%d", time.gmtime())
    r = Researcher(dir=tmp / dom["id"] / "plain", tags=tags, sensitivity="public", fetch=fetch, today=today)
    r.sources["auto_qualifiers"]["list"] = []  # a plain search: nothing is added by code
    q = "+".join(tags[0].split())
    page = r.fetch_source(r.search_address(f"{q}+agent").replace("%2B", "+") + f"&per_page={r.sources['kinds']['search']['max_per_page']}")
    listing = "\n".join(f"{i['n']}. {i['id']}: {i['description']} (license {i.get('license')}, stars {i.get('stars')}, pushed {str(i.get('pushed_at'))[:10]})" for i in page["items"])
    prompt = f"Domain: {', '.join(tags)}.\nPublic repositories:\n{listing}\n\nName up to three that are worth showing as open-source tools for this domain. Reply with one JSON object {{\"choices\": [numbers]}} and nothing else."
    done = await model.complete("You help find open-source tools for a kind of work.", prompt)
    try:
        nums = [int(x) for x in json.loads(done.text[done.text.index("{"):done.text.rindex("}") + 1])["choices"]][:3]
    except (ValueError, KeyError, TypeError):
        nums = []
    valid, rej, failed = 0, Counter(), Counter()
    for n in nums:
        it = next((x for x in page["items"] if x["n"] == n), None)
        if it is None:
            failed["item_not_on_page"] += 1
            continue
        bad = r.reasons("search", it)
        if not bad:
            try:
                ok, reason, _ = r.domain_check("search", it)
            except Refused as exc:
                failed[exc.code] += 1
                continue
            bad = [] if ok else [reason]
        valid += not bad
        rej.update(bad)
    return {"domain": dom["id"], "model_calls": 1, "tokens_in": done.prompt_tokens, "tokens_out": done.completion_tokens, "searches": 1, "requests": len(r.urls), "attempted": len(nums) - sum(failed.values()),
            "valid": valid, "rejected": dict(rej), "failures": dict(failed), "unparsed_reply": not nums}


def slots_for(model) -> Slots:
    return Slots(load_slots(), local=lambda _n: model, remote=None, level="public", permit=lambda: (None, "stance_unanswered"), log=lambda t, p: None)


async def types_run(model, gold: list[dict], harness: bool) -> dict:
    names = sorted(LABELS) + ["none_fits"]
    right = wrong = asked = 0
    calls = tin = tout = 0
    fails: Counter = Counter()
    returned: Counter = Counter()
    rows = []
    for t in gold:
        s = slots_for(model)
        ctx = lambda order, t=t: "Request: " + t["text"] + "\nWhat each kind means:\n" + "\n".join(f"- {n}: {LABELS.get(n, 'the work needs audio, images, files or tools that no listed kind has')}" for n in order)  # noqa: E731
        try:
            if harness:
                d = await decide(s, "task_type", names, ctx)
                pick, state = d.resolved, ("asked_owner" if d.resolved is None else "answered")
            else:
                pick, state = (await s.ask("task_type", ctx(names), names)).choice, "answered"
        except SlotRefused as exc:
            pick, state = None, "failed"
            fails[exc.code] += 1
        calls += len(s.calls)
        tin += sum(c.prompt_tokens for c in s.calls)
        tout += sum(c.completion_tokens for c in s.calls)
        returned.update(c.model_returned or "unreported" for c in s.calls)
        if state == "answered":
            right += pick == t["type"]
            wrong += pick != t["type"]
        elif state == "asked_owner":
            asked += 1
        rows.append({"id": t["id"], "gold": t["type"], "pick": pick, "state": state})
    return {"tasks": len(gold), "right": right, "wrong": wrong, "asked_owner": asked, "failed": sum(fails.values()), "failures": dict(fails), "model_calls": calls, "tokens_in": tin, "tokens_out": tout,
            "models_returned": dict(returned), "rows": rows}


def rate(rows: list[dict]) -> dict:
    att, val = sum(r["attempted"] for r in rows), sum(r["valid"] for r in rows)
    return {"valid": val, "attempted": att, "rate": round(val / att, 3) if att else None, "domains": len(rows), "domains_with_no_attempt": sum(1 for r in rows if not r["attempted"])}


async def run_all(model, domains: list[dict], gold: list[dict], *, only: str | None = None, fetch: Callable = real_fetch, log: Callable[[str, Any], None] = lambda k, v: None) -> dict:
    out: dict = {}
    tmp = Path(tempfile.mkdtemp(prefix="measure-"))
    if only in (None, "types"):
        out["types_harness"] = await types_run(model, gold, True)
        out["types_plain"] = await types_run(model, gold, False)
        log("types", {k: {x: v[x] for x in ("right", "wrong", "asked_owner", "failed")} for k, v in out.items() if k.startswith("types_")})
    if only in (None, "leads"):
        h: list[dict] = []
        p: list[dict] = []
        for d in domains:
            for acc, fn in ((h, leads_with_harness), (p, leads_plain)):
                try:
                    acc.append(await fn(model, d, tmp, fetch=fetch))
                except Exception as exc:  # noqa: BLE001 - reported as a failure of this domain, not hidden
                    acc.append({"domain": d["id"], "attempted": 0, "valid": 0, "error": f"{type(exc).__name__}: {str(exc)[:100]}"})
                log("harness" if fn is leads_with_harness else "plain", acc[-1])
        out["leads_harness"] = {"summary": rate(h), "rows": h}
        out["leads_plain"] = {"summary": rate(p), "rows": p}
    return out

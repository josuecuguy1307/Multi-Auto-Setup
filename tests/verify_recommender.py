"""Verification runs of the recommender (not collected by pytest): the held-out cases fixed BEFORE the build, plus one finance sample, through the real coordinator, a real node over loopback TLS and the
real MCP server (SIMULATED-LOCAL). What is REAL here: the web fetches (api.github.com on this Mac, the capture bytes are stored by hash) and, where the log says so, the harness model (local model on the local
Ollama). What is played: the node's own model answers and the owner's hands: NO owner answer is given in these runs, so no task is created and nothing is dispatched.

Run: PYTHONPATH=src .venv/bin/python tests/verify_recommender.py [case ...] [--scripted]   Writes live_results/recommender/<case>.json"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from acceptance_world import build  # noqa: E402
from test_acceptance_run import sc, text  # noqa: E402

from envrec.model import ScriptedModel, load_profile, make_adapter  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "live_results" / "recommender"
MODELS = Path.home() / "Desktop" / "env-recognition" / "config" / "models.yaml"  # the shipped file; the gitignored local profiles sit beside it
CLAUDE = [{"id": "claude", "name": "Claude Code", "set_up": True}, {"id": "opencode", "name": "OpenCode", "set_up": True}]

CASES = {
    "legal_confidential": dict(desc="contract review", domain=["legal"], touches=["legal"], profile={"confidential": ["finance", "legal"]}, chat="local", clis=None,
                               text="Review this synthetic services agreement for unusual indemnity and termination clauses.",
                               expect="only local candidates, never a subscription or cloud access; nothing measured on this task type, so the typed refusal provisional_only"),
    "data_science_public_csv": dict(desc="exploratory data analysis", domain=["data science"], touches=["none"], profile={}, chat="cloud", clis=CLAUDE,
                                    text="Describe the columns of a public CSV of city bike trips and suggest three charts.",
                                    expect="a default and a runner-up with reasons; the Claude plan shown as a candidate with its cost class (subscription) and waiting for the owner's clearance"),
    "grant_proposal_untagged": dict(desc="grant writing", domain=None, touches=["none"], profile={}, chat="cloud", clis=None,
                                    text="Draft the aims section of a grant proposal about soil carbon.",
                                    expect="the ladder fires: known data names nothing tagged, a hypothesis lead is saved with its source and never the default, ONE owner question"),
    "finance_public": dict(desc="financial statement analysis", domain=["finance"], touches=["none"], profile={"confidential": ["personal"]}, chat="cloud", clis=None,
                           text="Summarise the public annual report of a listed company: revenue, margin and risks.",
                           expect="same mechanism as the other domains with zero finance code: leads for the finance tag from public sources, never the default, ONE owner question"),
}


async def run_case(name: str, spec: dict, scripted: bool) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix=f"recver-{name}-"))
    w, c, nid, host, teardown = await build(tmp, clis=spec["clis"])
    try:
        p = c.services.profile
        for k, v in spec["profile"].items():
            p.set(k, v)
        if "confidential" not in spec["profile"]:
            p.set("confidential", ["finance"]) if name != "finance_public" else None
        p.apply(c.services.policy)
        if scripted:
            tags = spec["domain"] or [spec["desc"]]
            q = "+".join(tags[0].split()) + "+agent"
            c.services.recommend_model = ScriptedModel([json.dumps({"action": "call_tool", "tool": t, "args": a, "why": "next"}) for t, a in [("read_known", {}), ("choose_type", {"task_type": "text", "why": "Plain drafting."}),
                                                       ("fetch_source", {"url": f"https://api.github.com/search/repositories?q={q}+license:mit&per_page=5"}), ("record_candidate", {"capture": "p1", "item": "1", "subtype": "own_harness", "why": "The first public result for the domain tag."}),
                                                       ("finish_recommendation", {"summary": "Looked."})]])
            model_label = "SCRIPTED harness model"
        else:
            prof, opts = load_profile(MODELS, "local-profile")
            c.services.recommend_model = make_adapter(prof, opts)
            model_label = f"REAL harness model {prof.model_id} on local Ollama"
        # the fetches are REAL (api.github.com); only the owner and the node's own model are played
        who = host.local if spec["chat"] == "local" else host
        args = {"text": spec["text"], "task_type": spec["desc"], "touches": spec["touches"]}
        if spec["domain"]:
            args["domain"] = spec["domain"]
        t0 = time.time()
        r = await who.tool("mas_create_task", **args)
        res = sc(r)
        job = None
        if res.get("job_id"):
            for _ in range(900):
                job = c.services.jobs[res["job_id"]]
                if job.state != "researching":
                    break
                await asyncio.sleep(0.2)
        final = job.text if job else text(r)
        state = job.state if job else ("question" if res.get("question") else "refused")
        run = c.services.recommend_runs.get(res.get("job_id", ""))
        researcher = run["researcher"] if run else None
        leads = [e for e in c.services.catalog.values() if e.get("origin") == "researched"]
        actors: Counter = Counter(e.effective_tag for e in c.log.events())
        if run:
            actors += Counter(e.effective_tag for e in run["store"].events())
        q = [i for log in c.harness._logs.values() for i in log if i.kind == "ask_choice" and i.extra.get("state") == "pending" and str(i.extra.get("base", "")).startswith("recommend:")]
        out = {
            "case": name, "expected_before_the_run": spec["expect"], "labels": {"web fetches": "REAL" if researcher and researcher.urls else "none made", "coordinator, node, MCP": "SIMULATED-LOCAL", "harness model": model_label if run else "not used (known data was enough or research was not possible)",
                                                                                 "owner": "NOT PLAYED: no answer was given, no task created", "real Lenovo": "UNTESTED"},
            "seconds": round(time.time() - t0, 1), "job_state": state, "tool_ok": res.get("ok"), "text_at_first_answer": text(r)[:1500], "final_text": final[:3500],
            "questions_pending": [{"intent": i.id, "options": [o["id"] for o in i.payload["options"]], "details": [o.get("detail", "") for o in i.payload["options"]]} for i in q],
            "urls_requested": researcher.urls if researcher else [],
            "captures": {ref: {"url": cap["url"], "sha256": cap["sha256"], "items": len(cap["items"])} for ref, cap in (researcher.captures.items() if researcher else [])},
            "leads_recorded": [{k: e.get(k) for k in ("id", "evidence_state", "license", "source_url", "domains", "retrieved", "fetched")} for e in leads],
            "tool_events_of_the_harness_model": [(e.payload.get("tool"), e.payload.get("ok"), str(e.payload.get("error", ""))[:120]) for e in run["store"].events() if e.type == "tool_result"] if run else [],
            "actors": dict(actors), "claude_code_events": actors.get("CLAUDE-CODE", 0),
            "route_models_offered": [d for i in q for d in [o.get("detail", "") for o in i.payload["options"]]],
            "task_text_in_any_request": any(wd in " ".join(researcher.urls).lower() for wd in ("indemnity", "termination", "soil", "carbon", "bike", "annual report")) if researcher else False,
            "tasks_created": len(c.services.tasks), "dispatches": len(c.log.events("dispatch")),
        }
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / f"{name}{'_scripted' if scripted else ''}.json").write_text(json.dumps(out, indent=1, default=str))
        return out
    finally:
        await teardown()


async def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    scripted = "--scripted" in sys.argv
    for name in args or list(CASES):
        o = await run_case(name, CASES[name], scripted)
        print(f"== {name}: {o['job_state']} | urls {len(o['urls_requested'])} | leads {[x['id'] for x in o['leads_recorded']]} | actors {o['actors']} | {o['labels']['harness model']}")
        print(o["final_text"][:1800])
        print()


asyncio.run(main())

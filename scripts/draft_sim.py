"""SIMULATED-LOCAL verification of protocol-draft with the real local model: a coordinator on its own port and state dir, a simulated network built from two stored REAL analyses (a Windows 11
laptop and the coordinator Mac), one draft, one correction by talking, one approval. Whoever plays the owner here is the OPERATOR: its correction and approval are tagged CLAUDE-CODE, never OWNER.
Usage: python -I scripts/draft_sim.py <state-dir> <models-file> <profile> "<correction text>" """
from __future__ import annotations

import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "src")]

from envrec_coordinator.server import Coordinator  # noqa: E402


def stored(path: str, node: str) -> dict:
    d = json.loads((ROOT / "fixtures" / "probes" / path).read_text())
    facts = {k: {"value": v["value"], "status": v["status"], "probe": v["probe"]} for k, v in d["facts"].items()}
    facts["analysis.finished"] = {"value": True, "status": "tested", "probe": "fixture"}
    return {"run": f"fixture-{node}", "node": node, "stored_at": 1.0, "facts": facts}


NET = [{"node_id": "n-win", "name": "win-laptop", "stored": stored("windows-real/typed_facts.redacted.json", "n-win")},
       {"node_id": "n-mac", "name": "mac-coordinator", "stored": stored("darwin-real/typed_facts.json", "n-mac")}]


def show(r, label: str) -> None:
    v = r
    print(f"\n=== {label}: status={v.status} reason={v.reason!r}")
    print("summary:", json.dumps(v.summary))
    print("actors:", dict(Counter(e.effective_tag for e in v.store.events())))
    for e in v.store.events():
        if e.type in ("action_chosen", "tool_result", "protocol_state", "action_rejected", "owner_correction", "owner_approve_draft", "owner_answer"):
            p = e.payload
            print(f"  [{e.effective_tag}] {e.type} {p.get('tool') or ''} {str(p.get('error') or p.get('to') or p.get('text') or p.get('question') or '')[:150]}")
    if v.result and v.result.get("text"):
        print(v.result["text"])


async def main(state: str, models: str, profile: str, correction: str) -> None:
    c = Coordinator(Path(state), "127.0.0.1", 7843, discovery_port=None, model_profile=profile, models_file=Path(models))
    await c.start()
    try:
        async def answerer(r):
            while not r.finished:
                if r.adapter and r.adapter.pending:
                    p = r.adapter.pending
                    print(f"  (the model asked {p.qid}; the operator answers the first option, tagged CLAUDE-CODE)")
                    c.answer_run(r.id, p.qid, p.options[0]["id"], "claude-code")
                await asyncio.sleep(0.2)
        r1 = c.runs.start_draft("claude-code", network=lambda: NET)
        w = asyncio.create_task(answerer(r1))
        await r1.task
        w.cancel()
        show(r1, "round 1: the draft")
        if r1.status != "done":
            print("\nround 2 skipped: round 1 produced no draft to correct")
            return
        r2 = c.runs.start_draft("claude-code", correction=correction, network=lambda: NET)
        w = asyncio.create_task(answerer(r2))
        await r2.task
        w.cancel()
        show(r2, "round 2: after the correction")
        out = c.runs.approve_draft(r2.id, "claude-code") if r2.status == "done" else None
        print("\napproval output:", json.dumps(out, ensure_ascii=False, indent=1)[:1800] if out else "nothing to approve")
        print("profile:", json.dumps(c.runs.profile_view(), ensure_ascii=False)[:800])
        print("actors after approval:", dict(Counter(e.effective_tag for e in r2.store.events())))
    finally:
        await c.stop()


if __name__ == "__main__":
    asyncio.run(main(*sys.argv[1:5]))

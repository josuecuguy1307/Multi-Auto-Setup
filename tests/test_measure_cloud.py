"""The cloud conditions of the measurement, implemented end to end and run here against a mock of the cloud API (SIMULATED-LOCAL): the owner approves ONE question, the harness asks the cloud model through the node
that holds the key, the same fixed tasks and domains are scored by the same code, every call keeps the model id the API returned and its tokens, and a cap bounds the spend. The REAL run needs the Lenovo online:
the exact steps are in docs/KNOWN_GAPS.md (for the conductor)."""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import httpx
from acceptance_world import SENTINEL, build
from test_owner_profile import CH
from test_research import GOOD, serve
from test_slots import owner_answers_cloud_use

from envrec_coordinator.slots import load_slots

FIX = Path(__file__).parent / "fixtures" / "recommender"
GOLD = {t["text"]: t["type"] for t in json.loads((FIX / "gold_task_types.json").read_text())["tasks"]}


def run(coro):
    return asyncio.run(coro)


def cloud_reply(prompt: str) -> str:
    if "Public repositories:" in prompt:
        return json.dumps({"choices": [1]})
    for text, typ in GOLD.items():
        if text in prompt:
            return json.dumps({"choice": typ})
    return "not an action"  # the research loop's menu: this mock does not play it


def with_cloud(w, ctx, calls):
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        calls.append(body["messages"][0]["content"][:60])
        return httpx.Response(200, json={"id": "c1", "model": "mock-cloud-model-returned", "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
                                         "choices": [{"message": {"content": cloud_reply(body["messages"][0]["content"])}}]})
    ctx["transport"] = httpx.MockTransport(handler)


def test_the_owner_approves_one_question_then_the_cloud_is_measured_through_the_node_and_the_numbers_are_kept(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            calls: list = []
            with_cloud(w, w.agent.runner.ctx, calls)
            c.services.research_fetch = lambda url, max_bytes=None: serve({"https://api.github.com/search/repositories": GOOD}, url) if "search" not in url else (True, GOOD, "")
            r0 = c.services.measure_recommender(CH)
            assert not r0["ok"] and "does not allow" in r0["text"] and not calls  # no recorded answer on cloud use: nothing is asked
            owner_answers_cloud_use(c, "allowed")
            r = c.services.measure_recommender(CH)
            assert r["ok"] and "the cap is 200" in r["text"] and "reading public GitHub pages" in r["text"] and not calls  # the question names the spend; nothing is asked before the answer
            c.harness.decide(CH, r["intent_id"], "approve")
            job = c.services.jobs[r["job_id"]]
            for _ in range(600):
                if job.state in ("done", "failed"):
                    break
                await asyncio.sleep(0.1)
            assert job.state == "done", job.text
            saved = json.loads(Path(job.data["file"]).read_text())
            assert saved["cloud_types_plain"]["right"] == 12 and saved["cloud_types_plain"]["tasks"] == 12 and saved["cloud_types_harness"]["right"] == 12  # the mock answers right: the plumbing and the scoring work
            assert saved["models_returned_by_the_api"] == ["mock-cloud-model-returned"] and saved["cloud_calls"] == len(calls) <= load_slots()["measurement"]["max_calls"] and saved["tokens_in"] > 0
            assert "mock-cloud-model-returned" in job.text and "12/12" in job.text and not any(SENTINEL in json.dumps(saved) for _ in [0])
        finally:
            await teardown()
    run(go())


def test_declining_asks_nothing_and_the_call_cap_stops_the_spend(tmp_path, monkeypatch):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            calls: list = []
            with_cloud(w, w.agent.runner.ctx, calls)
            owner_answers_cloud_use(c, "allowed")
            r = c.services.measure_recommender(CH)
            c.harness.decide(CH, r["intent_id"], "reject")
            await asyncio.sleep(0.2)
            assert c.services.jobs[r["job_id"]].state == "declined" and not calls
            import envrec_coordinator.measure_tools as MT
            real = MT.load_slots
            monkeypatch.setattr(MT, "load_slots", lambda: {**real(), "measurement": {"max_calls": 5}})
            r2 = c.services.measure_recommender(CH)
            c.harness.decide(CH, r2["intent_id"], "approve")
            job = c.services.jobs[r2["job_id"]]
            for _ in range(300):
                if job.state in ("done", "failed"):
                    break
                await asyncio.sleep(0.1)
            assert len(calls) <= 5  # the cap is the data's, and it holds
        finally:
            await teardown()
    run(go())

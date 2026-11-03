"""P6: the flagship chain reads the computer itself when no usable analysis is stored (or it is older than the data's age), then goes on: the harness chains the steps, the chat model does not have to remember.
SIMULATED-LOCAL like test_flagship.py: real coordinator, node and MCP server; the analysis run and the setup run are replaced by their recorded outcomes; the owner's answers are played by the test."""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

from acceptance_world import build, owner_yes, set_server, wait_job
from test_acceptance_run import sc, text
from test_flagship import CLOUD, FACTS, NAMES, SECRET, Seen, fake_install, make_folder, pending, store_analysis, until

from envrec_coordinator.chain import load_chains


def run(coro):
    return asyncio.run(coro)


def fake_analysis(c, nid, calls, *, ok=True, age=0.0):
    def start_analysis(node_id, via="web", **kw):
        calls.append((node_id, via))
        fut = asyncio.get_event_loop().create_future()
        if ok:
            store_analysis(c, node_id)
            doc = json.loads((c.runs.analyses / f"{node_id}.json").read_text())
            doc["stored_at"] = time.time() - age
            (c.runs.analyses / f"{node_id}.json").write_text(json.dumps(doc))
        fut.set_result(None)
        return SimpleNamespace(id="ana1", status="done" if ok else "failed", reason="" if ok else "the machine is not connected right now", task=fut)
    c.runs.start_analysis = start_analysis


def test_the_chain_data_names_the_step_and_the_age():
    f = load_chains()["task_with_inputs"]
    assert f["steps"]["analyze"]["on"] == {"done": "propose_setup", "failed": "end"} and f["steps"]["propose_setup"]["on"]["no_analysis"] == "analyze" and f["max_visits"]["analyze"] == 1 and f["analysis_max_age_s"] == 604800


def test_from_no_stored_analysis_to_the_final_answer_without_the_chat_remembering_anything(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path, key=False, server=False)
        try:
            d = make_folder(tmp_path)
            c.services.profile.set("confidential", ["personal"]); c.services.profile.apply(c.services.policy)
            assert c.runs.stored_analysis(nid) is None
            calls: list = []
            fake_analysis(c, nid, calls)
            installs: list = []
            fake_install(c, nid, installs)
            w.script.accuracy[("llamacpp-local", "local-hardware", "text")] = 10
            w.script.answers.append((lambda e, a, p: e == "llamacpp-local" and "Contents of the folder" in p, lambda e, a, p: f"Mostly work files: {NAMES[0]} looks like a draft."))
            seen = Seen()
            r = seen.tool_result(await host.tool("mas_create_task", text="Analyze the files on my Lenovo's Desktop", task_type="file analysis", touches=["none"], inputs=[{"kind": "folder", "machine": "lenovo", "folder": str(d)}]))
            cid = sc(r)["chain"]
            (q,) = pending(c, f"chain:{cid}:in")
            seen.reply(c.harness.decide(CLOUD, q.id, "approve"))
            j = await until(c, cid, ("awaiting_answer",))
            assert calls == [(nid, "harness")] and "Recommended:" in j.text  # the chain read the computer by itself, then proposed the setup from what it found
            steps = [(e.payload["step"], e.payload["outcome"]) for e in c.log.events("chain_step") if e.payload.get("chain") == cid]
            assert ("propose_setup", "no_analysis") in steps and ("analyze", "done") in steps
            (sq,) = pending(c, f"chain:{cid}:setup")
            seen.reply({"next": c.harness.decide(CLOUD, sq.id, "use_default")})
            j = await until(c, cid, ("awaiting_approval",))
            seen.tool_result(await owner_yes(host, j.intent_id))
            j = await until(c, cid, ("task_created",), timeout=60)
            tid = j.data["task_id"]
            seen.tool_result(await owner_yes(host, c.services.tasks[tid]["intent_id"]))
            done = seen.tool_result(await wait_job(host, tid, until=("done_unchecked", "done", "failed", "refused"), timeout=60))
            assert "Answer from llamacpp-local" in text(done) and "[name withheld]" in text(done)
            blob = seen.blob()
            for n in NAMES + [SECRET]:
                assert n.lower() not in blob.lower(), n  # the privacy boundary of the flow did not change
            req = [e for e in c.log.events("analysis_requested") if e.payload.get("via") == "harness"]
            assert len(req) == 0 or all(e.effective_tag != "OWNER" for e in req)  # the harness's own read is not the owner's press
            from collections import Counter
            tags = Counter(e.effective_tag for e in c.log.events())
            assert tags.get("CLAUDE-CODE", 0) == 0
        finally:
            await teardown()
    run(go())


def test_a_stale_analysis_is_read_again_and_a_fresh_one_is_not(tmp_path):
    async def go():
        for age, expect in ((8 * 86400, 1), (3600, 0)):
            w, c, nid, host, teardown = await build(tmp_path / str(age), key=False, server=False)
            try:
                d = make_folder(tmp_path / str(age))
                c.services.profile.set("confidential", ["personal"]); c.services.profile.apply(c.services.policy)
                store_analysis(c, nid)
                doc = json.loads((c.runs.analyses / f"{nid}.json").read_text())
                doc["stored_at"] = time.time() - age
                (c.runs.analyses / f"{nid}.json").write_text(json.dumps(doc))
                calls: list = []
                fake_analysis(c, nid, calls)
                r = await host.tool("mas_create_task", text="Analyze the files", task_type="file analysis", touches=["none"], inputs=[{"kind": "folder", "machine": "lenovo", "folder": str(d)}])
                cid = sc(r)["chain"]
                (q,) = pending(c, f"chain:{cid}:in")
                c.harness.decide(CLOUD, q.id, "approve")
                await until(c, cid, ("awaiting_answer",))
                assert len(calls) == expect, age
            finally:
                await teardown()
    run(go())


def test_an_analysis_that_fails_ends_the_chain_and_says_so_and_one_that_stores_nothing_is_not_repeated(tmp_path):
    async def go():
        for ok, state in ((False, "failed"), (True, "failed")):
            w, c, nid, host, teardown = await build(tmp_path / str(ok), key=False, server=False)
            try:
                d = make_folder(tmp_path / str(ok))
                c.services.profile.set("confidential", ["personal"]); c.services.profile.apply(c.services.policy)
                calls: list = []
                fake_analysis(c, nid, calls, ok=ok)
                if ok:  # "done" but nothing usable is stored: the read happens once, then the chain says there is no analysis
                    real = c.runs.start_analysis

                    def empty(node_id, via="web", **kw):
                        run_ = real(node_id, via, **kw)
                        (c.runs.analyses / f"{node_id}.json").unlink(missing_ok=True)
                        return run_
                    c.runs.start_analysis = empty
                r = await host.tool("mas_create_task", text="Analyze the files", task_type="file analysis", touches=["none"], inputs=[{"kind": "folder", "machine": "lenovo", "folder": str(d)}])
                cid = sc(r)["chain"]
                (q,) = pending(c, f"chain:{cid}:in")
                c.harness.decide(CLOUD, q.id, "approve")
                j = await until(c, cid, (state,))
                assert len(calls) == 1 and not c.services.tasks and "did not finish" in j.text
            finally:
                await teardown()
    run(go())


def test_a_run_started_by_the_harness_is_not_an_owner_event(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path, key=False, server=False)
        try:
            run_ = c.runs.start_analysis(nid, "harness")  # no model is configured here: the run ends at once, which is enough to see who asked
            ev = [e for e in c.log.events("analysis_requested") if e.payload.get("via") == "harness"]
            assert ev and ev[0].effective_tag != "OWNER"
            assert run_.store is None or not any(e.tag == "OWNER" for e in run_.store.events() if e.type == "run_requested")
        finally:
            await teardown()
    run(go())

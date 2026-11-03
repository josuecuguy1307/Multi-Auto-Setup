"""The proof of execution (P5, finding K): every environment run carries, inside the NODE's signed reply, the node's host, start and end on its own clock, the API's response id and `model` field, token usage
when present, bytes sent and received, and hashes of the input and the output. The coordinator checks it before it shows anything and cannot write one: a result without a proof says so, an altered one
does not verify, a proof for another output does not verify. mas_job, mas_explain_route and an exam's job show it in text. SIMULATED-LOCAL: the API is a mock transport that answers like a real one would."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json

import httpx
from acceptance_world import LOCAL_URL, META_URL, build, owner_yes, wait_job
from test_acceptance_run import sc, text

from envrec_net import envrun as E

API_ANSWER = {"id": "chatcmpl-REAL-123", "model": "muse-spark-1.3-2026-09-02", "choices": [{"message": {"content": "A brief."}}], "usage": {"prompt_tokens": 17, "completion_tokens": 5, "total_tokens": 22}}


def run(coro):
    return asyncio.run(coro)


def api_world_handler(w):
    """Replace the world's model server by one whose answers carry what a real API's do."""
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        prompt = body["messages"][0]["content"]
        local = str(req.url) == LOCAL_URL
        status, out = w.script.reply("llamacpp-local" if local else "meta-model-api", "local-hardware" if local else "meta-model-api:api-key", prompt, model=body.get("model"))
        if local:
            return httpx.Response(200, json={"choices": [{"message": {"content": out}}]})  # a local server answers with no id, no model field, no usage
        return httpx.Response(200, json={**API_ANSWER, "choices": [{"message": {"content": out}}]})
    return handler


def test_the_proof_functions_accept_only_what_matches_and_say_why_when_it_does_not():
    out = "A brief."
    proof = {"recorded_by": "node", "node_host": "lenovo", "started_at": 1000.0, "ended_at": 1002.5, "wall_s": 2.5, "api": {"response_id": "r1", "model": "m-1", "usage": {"prompt_tokens": 1, "completion_tokens": 2}},
             "model_requested": "m", "bytes_sent": 10, "bytes_received": 20, "input_sha256": "ab", "output_sha256": hashlib.sha256(out.encode()).hexdigest()}
    proof["digest"] = E.proof_digest(proof)
    assert E.verify_proof(proof, out) == (True, "")
    assert "the model field in the API's answer: m-1 (the route asked for m)" in E.proof_line(proof, True, "") and "response id r1" in E.proof_line(proof, True, "")
    forged = copy.deepcopy(proof)
    forged["api"]["model"] = "a-bigger-model"
    assert E.verify_proof(forged, out) == (False, "the proof does not match its own digest")
    assert E.verify_proof(proof, "another output")[1] == "the proof is for another output than the one shown"
    assert E.verify_proof(None, out) == (False, "the node recorded no proof of this run") and E.verify_proof({"recorded_by": "coordinator"}, out)[0] is False
    assert E.proof_line({}, False, "the node recorded no proof of this run") == "Proof of execution: the node recorded no proof of this run."


def test_an_api_run_carries_the_apis_own_id_model_and_usage_and_the_chat_reads_it_in_text(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            c.services.profile.set("local_first", "any"); c.services.profile.apply(c.services.policy)  # the owner prefers the API: the rule picks the cloud route
            r = await host.tool("mas_create_task", text="Write a brief: the sky is blue.", task_type="summarize", touches=["none"])
            assert "meta-model-api" in text(r)
            await owner_yes(host, sc(r)["intent_id"])
            j = await wait_job(host, sc(r)["task_id"], timeout=60)
            proof = c.services.tasks[sc(r)["task_id"]]["attempts"][0]["proof"]
            assert proof["ok"] is True
            sm = proof["summary"]
            assert sm["response_id"] == "chatcmpl-test-0001" and sm["model_returned"] == "muse-spark-1.3-returned" and sm["model_requested"] == "muse-spark-1.3" and sm["usage"]["total_tokens"] == 22 and sm["node_host"]
            assert sm["ended_at"] >= sm["started_at"] > 1_700_000_000 and sm["bytes_sent"] > 0 and sm["bytes_received"] > 0
            t = text(j)
            assert "Proof recorded by the node" in t and "on its own clock" in t and "response id chatcmpl-test-0001" in t and "muse-spark-1.3-returned (the route asked for muse-spark-1.3)" in t and "tokens 17 in, 5 out" in t
            ex = await host.tool("mas_explain_route", task_id=sc(r)["task_id"])
            assert "Proof recorded by the node" in text(ex) and sc(ex)["proofs"]
        finally:
            await teardown()
    run(go())


def test_a_local_server_says_which_fields_it_did_not_report(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            r = await host.tool("mas_create_task", text="Write a brief: the sky is blue.", task_type="summarize", touches=["none"])
            assert "llamacpp-local" in text(r)
            await owner_yes(host, sc(r)["intent_id"])
            j = await wait_job(host, sc(r)["task_id"], timeout=60)
            line = c.services.tasks[sc(r)["task_id"]]["attempts"][0]["proof"]["line"]
            assert "response id not reported" in line and "not reported (a local server or a tool)" in line and "token usage: not reported" in line and "Proof recorded by the node" in text(j)
        finally:
            await teardown()
    run(go())


def test_the_coordinator_cannot_forge_a_proof_it_can_only_fail_to_find_or_fail_to_verify_one(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            r = await host.tool("mas_create_task", text="Write a brief: the sky is blue.", task_type="summarize", touches=["none"])
            await owner_yes(host, sc(r)["intent_id"])
            await wait_job(host, sc(r)["task_id"], timeout=60)
            rec = next(t for t in c.tasks.tasks.values() if t.task_type == "environment_run" and t.status == "done")
            good = rec.result
            assert c.services._proof_of(good)["ok"] is True
            bare = {k: v for k, v in good.items() if k != "proof"}
            assert c.services._proof_of(bare) == {"ok": False, "why": "the node recorded no proof of this run", "line": "Proof of execution: the node recorded no proof of this run.", "summary": None}  # nothing fills a missing proof
            edited = copy.deepcopy(good)
            edited["proof"]["node_host"] = "somewhere-else"
            assert c.services._proof_of(edited)["why"] == "the proof does not match its own digest"
            reattached = {**good, "output_text": "A different answer."}
            assert c.services._proof_of(reattached)["why"] == "the proof is for another output than the one shown"
            claimed = {**good, "proof": {**good["proof"], "recorded_by": "coordinator"}}
            assert c.services._proof_of(claimed)["ok"] is False
            # and the code that builds a proof lives on the node: the coordinator package never creates one
            import pathlib
            src = pathlib.Path(__file__).resolve().parents[1] / "src" / "envrec_coordinator"
            assert not [p for p in src.rglob("*.py") if '"recorded_by": "node"' in p.read_text() or "'recorded_by': 'node'" in p.read_text()]
        finally:
            await teardown()
    run(go())


def test_an_exam_on_an_api_access_keeps_what_the_node_recorded_for_each_prompt(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            w.script.accuracy[("meta-model-api", "meta-model-api:api-key", "text")] = 10
            r = await host.tool("mas_run_admission", environment="meta-model-api", task_type="text")
            await owner_yes(host, sc(r)["intent_id"])
            done = await wait_job(host, sc(r)["job_id"], until=("done", "aborted", "refused", "declined"), timeout=120)
            assert sc(done)["state"] == "done"
            np = c.services.jobs[sc(r)["job_id"]].data["node_proofs"]
            assert np["replies"] == np["with_proof"] == 10 and np["models_returned"] == ["muse-spark-1.3-returned"] and len(np["hosts"]) == 1
            assert "Recorded by the node for 10 of 10 prompts" in text(done) and "muse-spark-1.3-returned" in text(done)
            ev = [e.payload for e in c.log.events("admission_reply")]
            assert all(p["proof"]["response_id"] == "chatcmpl-test-0001" and "text" not in p["proof"] for p in ev)
        finally:
            await teardown()
    run(go())

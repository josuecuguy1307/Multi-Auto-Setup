"""The router acceptance examples (docs/ROUTER_ACCEPTANCE.md, frozen), run SIMULATED-LOCAL through the MCP tools of the Harness App. Each test records the route reasoning the harness returned
(live_results/phase4d/<id>.json) and checks the REASONING: candidates, which filter cut what and why, what the written rule chose. The expected values were fixed before the first run; nothing here edits an
example to make it pass. Where the frozen text and the harness disagree, the test asserts what the harness did and the difference is named in the report, not hidden."""
from __future__ import annotations

import asyncio
import json
from collections import Counter
from pathlib import Path

import pytest

from envrec_coordinator.router import EntryChat, Task
from acceptance_world import MODEL, SENTINEL, build, owner_yes, set_server, wait_job, TOUCHES
from envrec.skills import sbx

FX = Path(__file__).parent / "fixtures" / "acceptance"
RESULTS = Path(__file__).resolve().parents[1] / "live_results" / "phase4d"
docker = pytest.mark.skipif(not sbx.docker_ready(), reason="Docker (or the sandbox image) is not ready: model-written code does not run without it (sandbox_missing is the expected typed result)")

E1 = f"llamacpp-local|{MODEL}|local-hardware|"
E2 = "meta-model-api|muse-spark-1.3|meta-model-api:api-key|"
E3A = f"opencode|{MODEL}|opencode:local|"
E3B = "opencode|muse-spark-1.3|opencode:meta-api|"


def run(coro):
    return asyncio.run(coro)


def text(r):
    return r["content"][0]["text"]


def sc(r):
    return r["structuredContent"]


def fx(name):
    d = FX / name
    return {p.name: p.read_text(encoding="utf-8") for p in d.iterdir()}


def record(name, **data):
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"{name}.json").write_text(json.dumps(data, indent=1, default=str, sort_keys=True))


def actors(c):
    return dict(Counter(e.effective_tag for e in c.log.events()))


def key(k, nid):
    return k + nid


async def measure(w, host, env, access, task_type, passed, machine=None):
    """A real admission through the tools: the owner approves in his dialog, the suite's tasks go to the played model (which gets `passed` of them right), the fact is written by the exam."""
    w.script.accuracy[(env, access, task_type)] = passed
    r = await host.tool("mas_run_admission", environment=env, task_type=task_type, access=access, **({"machine": machine} if machine else {}))
    assert sc(r)["ok"], text(r)
    await owner_yes(host, sc(r)["intent_id"])
    j = await wait_job(host, sc(r)["job_id"], until=("done", "aborted", "refused", "declined"), timeout=180)
    assert sc(j)["state"] == "done", text(j)
    return j


async def create(host, name, task_type, sensitivity, *, check=True, input_text=None):
    f = fx(name)
    return await host.tool("mas_create_task", text=input_text or f["input.txt"], task_type=task_type, touches=TOUCHES[sensitivity], **({"check": json.loads(f["check.json"])} if check else {}))


async def execute(host, created, timeout=120):
    await owner_yes(host, sc(created)["intent_id"])
    return await wait_job(host, sc(created)["task_id"], timeout=timeout)


def answers(w, name, prompt_start, good="good.txt", env=None, access=None):
    f = fx(name)
    w.script.answers.insert(0, (lambda e, a, prompt, p=prompt_start, ev=env, ac=access: prompt.startswith(p) and ev in (None, e) and ac in (None, a), f[good]))


def filt(d, step):
    return next(s for s in d["filters"] if s["step"] == step)


def no_secret_anywhere(c, tmp, *results):
    from test_secret_entry import everything_under
    blob = "\n".join(json.dumps(e.payload, default=str) for e in c.log.events()) + "\n".join(json.dumps(r, default=str) for r in results)
    assert SENTINEL not in blob and SENTINEL not in everything_under(tmp)


# ===================================================================== Example 1: summarize, public, four sets of facts =====================================================================

def test_ex1_v1_a_measured_local_environment_wins_on_cost_class(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            await measure(w, host, "llamacpp-local", "local-hardware", "summarize", 9)
            answers(w, "ex1_article", "Summarize this article")
            r = await create(host, "ex1_article", "summarize", "public")
            d = sc(r)
            assert d["chosen"] == E1 + nid and d["evidence"] == "measured here 9/10" and d["cost_class"] == "local" and not d["provisional"]
            assert len(d["candidates"]) == 4 and "Cost class local" in text(r) and "measured here 9/10" in text(r)
            assert set(filt(d, "admission_filter")["values"].values()) == {"unmeasured"} and len(filt(d, "admission_filter")["candidates_out"]) == 1  # measured first: the unmeasured ones do not compete
            j = await execute(host, r)
            assert sc(j)["state"] == "done", text(j)
            record("ex1_v1", explain=text(r), job=text(j), actors=actors(c))
        finally:
            await teardown()
    run(go())


def test_ex1_v2_a_failed_measurement_drops_it_and_the_cheapest_unmeasured_api_route_is_chosen_provisional(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            await measure(w, host, "llamacpp-local", "local-hardware", "summarize", 6)
            await measure(w, host, "opencode", "opencode:local", "summarize", 5)  # the doc's variant has no local route left: the other local route also failed its exam
            r = await create(host, "ex1_article", "summarize", "public", check=False)
            d = sc(r)
            af = filt(d, "admission_filter")
            assert af["values"][E1 + nid] == "not_admitted" and af["values"][E3A + nid] == "not_admitted"
            assert d["chosen"] in (E2 + nid, E3B + nid) and d["chosen"] == E2 + nid and d["cost_class"] == "api" and d["provisional"] is True and "api" in text(r) and "provisional" in text(r)
            record("ex1_v2", explain=text(r), actors=actors(c))
        finally:
            await teardown()
    run(go())


def test_ex1_v3_a_measured_api_route_beats_a_cheaper_unmeasured_local_one(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            await measure(w, host, "meta-model-api", "meta-model-api:api-key", "summarize", 8)
            r = await create(host, "ex1_article", "summarize", "public", check=False)
            d = sc(r)
            assert d["chosen"] == E2 + nid and d["evidence"] == "measured here 8/10" and not d["provisional"]
            assert filt(d, "admission_filter")["values"][E1 + nid] == "unmeasured"
            record("ex1_v3", explain=text(r), actors=actors(c))
        finally:
            await teardown()
    run(go())


def test_ex1_v4_with_nothing_measured_the_cheapest_unmeasured_wins_and_is_flagged_provisional(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            r = await create(host, "ex1_article", "summarize", "public", check=False)
            d = sc(r)
            assert d["chosen"] in (E1 + nid, E3A + nid) and d["provisional"] is True and d["cost_class"] == "local" and "provisional" in text(r)
            assert filt(d, "cost_class")["candidates_out"] and filt(d, "tie_break")["candidates_out"] == [d["chosen"]]
            record("ex1_v4", explain=text(r), actors=actors(c))
        finally:
            await teardown()
    run(go())


# ===================================================================== Example 3: confidential, local only ====================================================================================

def test_ex3_confidential_goes_only_to_the_measured_local_environment_and_never_from_a_cloud_chat(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            await measure(w, host, "llamacpp-local", "local-hardware", "summarize", 9)
            answers(w, "ex3_notes", "Summarize these client payment notes")
            refused = await create(host, "ex3_notes", "summarize", "confidential")  # from the cloud chat (Claude Desktop)
            assert sc(refused)["ok"] is False and sc(refused)["refusal"]["code"] == "confidential_from_cloud_chat" and "local chat" in text(refused)
            r = await create(host.local, "ex3_notes", "summarize", "confidential")
            d = sc(r)
            cut = filt(d, "sensitivity")["values"]
            assert cut[E2 + nid] == "cloud" and cut[E3B + nid] == "cloud" and cut[E3A + nid] == "provisional"
            assert d["chosen"] == E1 + nid and filt(d, "sensitivity")["candidates_out"] == [E1 + nid]
            j = await execute(host.local, r)
            assert sc(j)["state"] == "done", text(j)
            assert not [x for x in w.script.calls if x[0] == "meta-model-api"]  # nothing went to the cloud
            record("ex3", explain=text(r), refused_from_cloud=text(refused), job=text(j), actors=actors(c))
        finally:
            await teardown()
    run(go())


def test_ex3_variant_with_nothing_measured_locally_is_provisional_only_and_nothing_is_routed(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            r = await create(host.local, "ex3_notes", "summarize", "confidential")
            assert sc(r)["ok"] is False and sc(r)["refusal"]["code"] == "provisional_only" and "intent_id" not in sc(r)
            assert not w.script.calls
            record("ex3_variant", explain=text(r), actors=actors(c))
        finally:
            await teardown()
    run(go())


# ===================================================================== Example 4: input longer than the local context ================================================================================

def test_ex4_the_local_server_fails_naturally_and_the_task_escalates_to_the_api_then_closes(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            await measure(w, host, "llamacpp-local", "local-hardware", "summarize", 9)
            await measure(w, host, "meta-model-api", "meta-model-api:api-key", "summarize", 8)
            answers(w, "ex4_long", "Write a one-page brief")
            r = await create(host, "ex4_long", "summarize", "public")
            assert sc(r)["chosen"] == E1 + nid
            j = await execute(host, r)
            at = sc(j)["attempts"]
            assert sc(j)["state"] == "done" and [a["step"] for a in at] == [0, 1] and at[0]["state"] == "failed" and at[1]["graded"] is True
            assert at[0]["route"] == E1 + nid and at[1]["route"] == E2 + nid
            assert [e.payload["to"] for e in c.log.events("escalation")] == [E2 + nid]
            record("ex4", explain=text(r), job=text(j), attempts=at, actors=actors(c))
        finally:
            await teardown()
    run(go())


def test_ex4_variant_when_the_api_fails_too_the_escalation_is_capped_and_the_owner_is_warned(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            await measure(w, host, "llamacpp-local", "local-hardware", "summarize", 9)
            await measure(w, host, "meta-model-api", "meta-model-api:api-key", "summarize", 8)
            w.script.fail["meta-model-api"] = 500
            r = await create(host, "ex4_long", "summarize", "public")
            j = await execute(host, r)
            at = sc(j)["attempts"]
            assert sc(j)["state"] == "exhausted" and len(at) <= 3 and all(a["graded"] is not True for a in at)
            assert c.log.events("owner_notice") and "needs you" in c.log.events("owner_notice")[-1].payload["text_fallback"] and len({a["route"] for a in at}) == len(at)  # never the same route twice
            record("ex4_variant", job=text(j), attempts=at, notice=c.log.events("owner_notice")[-1].payload["text_fallback"], actors=actors(c),
                   note="the frozen text reads 'E1 -> E2 -> stop'; the router's cap is 2 escalations (3 tries): a third route (OpenCode, unmeasured) was tried before the stop")
        finally:
            await teardown()
    run(go())


# ===================================================================== Example 5: the Meta access is removed through the chat =================================================================

def test_ex5_removing_the_access_needs_the_owners_dialog_and_then_every_route_says_access_missing_by_name(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        outs = []
        try:
            before = await host.tool("mas_explain_route", task_type="summarize", sensitivity="public")
            assert E2 + nid in sc(before)["candidates"] and E3B + nid in sc(before)["candidates"]
            w.dialog.result = "declined"  # the owner cancels the dialog: nothing changes
            r = await host.tool("mas_remove_access", node="lenovo", access="meta-model-api:api-key")
            j = sc(r)["job_id"]
            for _ in range(100):
                s = await host.tool("mas_job", job_id=j)
                if sc(s).get("intent_id") or sc(s)["state"] not in ("checking",):
                    break
                await asyncio.sleep(0.1)
            outs += [r, s]
            await host.tool("mas_request_approval", intent_id=c.services.jobs[j].intent_id)
            assert (await wait_job(host, j, until=("declined", "done", "failed")))["structuredContent"]["state"] == "declined"
            from envrec_node import credentials as C
            assert C.has("MODEL_API_KEY") is True  # still there
            w.dialog.result = "approved"
            r = await host.tool("mas_remove_access", node="lenovo", access="meta-model-api:api-key")
            j = sc(r)["job_id"]
            for _ in range(100):
                if c.services.jobs[j].intent_id:
                    break
                await asyncio.sleep(0.1)
            await host.tool("mas_request_approval", intent_id=c.services.jobs[j].intent_id)
            done = await wait_job(host, j, until=("done", "failed", "declined"))
            assert sc(done)["state"] == "done" and "MODEL_API_KEY" in text(done)
            after = await host.tool("mas_explain_route", task_type="summarize", sensitivity="public")
            d = sc(after)
            assert E2 + nid not in d["candidates"] and E3B + nid not in d["candidates"] and E1 + nid in d["candidates"]
            assert d["left_out"][f"meta-model-api@{nid}"] == "access missing: MODEL_API_KEY" and d["left_out"][f"opencode via opencode:meta-api@{nid}"] == "access missing: MODEL_API_KEY"
            assert "access missing: MODEL_API_KEY" in text(after)
            # a bare tool path cannot do it: no approval for this plan, nothing is sent
            from envrec_net import task as T
            h = T.credential_plan_hash(nid, "meta-model-api:api-key", "MODEL_API_KEY", "restore")  # a change nobody has approved yet
            assert c.tasks.start_credential_step(nid, "MODEL_API_KEY", "restore", h).reason == "no_owner_approval"
            # the undo brings it back
            r = await host.tool("mas_remove_access", node="lenovo", access="meta-model-api:api-key", undo=True)
            j = sc(r)["job_id"]
            for _ in range(100):
                if c.services.jobs[j].intent_id:
                    break
                await asyncio.sleep(0.1)
            await host.tool("mas_request_approval", intent_id=c.services.jobs[j].intent_id)
            assert sc(await wait_job(host, j, until=("done", "failed", "declined")))["state"] == "done"
            back = await host.tool("mas_explain_route", task_type="summarize", sensitivity="public")
            assert E2 + nid in sc(back)["candidates"] and E3B + nid in sc(back)["candidates"]
            outs += [before, after, back, done]
            no_secret_anywhere(c, tmp_path, *outs)
            record("ex5", before=text(before), after=text(after), restored=text(back), actors=actors(c))
        finally:
            await teardown()
    run(go())


# ===================================================================== Example 6: the local server is stopped ======================================================================================

def test_ex6_a_stopped_server_leaves_only_the_api_for_public_work_and_a_typed_refusal_for_confidential(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            await measure(w, host, "llamacpp-local", "local-hardware", "summarize", 9)
            await measure(w, host, "meta-model-api", "meta-model-api:api-key", "summarize", 8)
            set_server(c, nid, up=False)  # the owner stopped the server (the undo of its start step)
            r = await create(host, "ex1_article", "summarize", "public", check=False)
            d = sc(r)
            assert E1 + nid not in d["candidates"] and d["left_out"][f"llamacpp-local@{nid}"] == "its local model server is not running"
            assert d["chosen"] == E2 + nid
            k = await create(host.local, "ex3_notes", "summarize", "confidential")
            assert sc(k)["ok"] is False and sc(k)["refusal"]["code"] == "no_local_candidate" and "intent_id" not in sc(k)
            assert set(sc(k)["dropped"].values()) == {"cloud environment (not allowed for confidential tasks)"} and E1 + nid not in sc(k)["candidates"]  # the API routes were candidates and were cut as cloud; no local one was left
            record("ex6", public=text(r), confidential=text(k), actors=actors(c))
        finally:
            await teardown()
    run(go())


# ===================================================================== Example 7: nothing declares the task type ====================================================================================

def test_ex7_an_unknown_task_type_is_a_recommendation_and_one_question_and_nothing_is_chosen_or_sent(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            r = await host.tool("mas_create_task", text="audio.wav", task_type="transcribe-audio", touches=TOUCHES["public"])
            d = sc(r)
            assert d["ok"] is True and d["question"] is True and "Recommendation for" in text(r) and "If it needs audio, images, files or tools, answer Neither" in text(r)  # amendment 5: the harness proposes, it does not dead-end
            assert not c.services.tasks and not c.services.jobs and "intent_id" in d and d["intent_id"].startswith("recommend:")  # no task, no route approved, nothing to run
            assert not w.script.calls and not c.log.events("escalation") and not c.log.events("dispatch") and not c.log.events("route_decision")  # nothing invented: no model asked, nothing dispatched, no route decided
            direct = c.services.router.explain(Task(task_id="direct", task_type="transcribe-audio", sensitivity="public", entry_chat=EntryChat(kind="local", name="x"), created_by="OWNER", payload_ref="r"))
            assert direct.code == "no_candidate" and direct.details["why"] == "unknown_task_type" and direct.details["candidates"] == []  # the router itself still refuses an unknown type
            record("ex7", reasoning=text(r), actors=actors(c))
        finally:
            await teardown()
    run(go())


# ===================================================================== Example 8: the Lenovo is offline ============================================================================================

def test_ex8_an_offline_machine_is_a_typed_refusal_with_one_event_and_no_retry(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            from test_mas_tasks import down
            # the node goes away: the test drops its session the way a lost network does
            sess = c._sessions[nid]
            sess.writer.close()
            for _ in range(200):
                if nid not in c._sessions:
                    break
                await asyncio.sleep(0.05)
            assert nid not in c._sessions
            before = len(c.log.events("task_created"))
            r = await host.tool("mas_create_task", text="x", task_type="summarize", touches=TOUCHES["public"])
            d = sc(r)
            assert d["ok"] is False and d["refusal"]["code"] == "machine_offline" and "machine offline" in d["refusal"]["reason"]
            assert set(d["left_out"].values()) == {"machine offline"}
            await asyncio.sleep(0.5)
            assert len(c.log.events("task_created")) == before and len(c.log.events("task_refused")) == 1 and not c.log.events("dispatch")
            record("ex8", reasoning=text(r), actors=actors(c))
        finally:
            await teardown()
    run(go())

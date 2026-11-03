"""Ids and supervision (P3, findings G and H): every question shows the id it is answered with; every approved action says "job id for mas_job: <id>" and its state; mas_job follows a task, an exam or a
run to the end; mas_activity lists what is open; `mas runs` / `mas run <id>` read the same from a terminal; an exam prompt that times out frees its place on the node and a busy node is asked again.
Nothing here plays the owner: the world's dialog is the owner's gesture, and every step reads only the tool text. SIMULATED-LOCAL."""
from __future__ import annotations

import asyncio
import json
import re
import time

import pytest
from acceptance_world import build, owner_yes, wait_job
from test_acceptance_run import sc, text

from envrec_coordinator.admission.facts import Combo
from envrec_coordinator.admission.exam import RunnerAborted
from envrec_coordinator.runs import AgentRun


def run(coro):
    return asyncio.run(coro)


def ids(t: str, label: str) -> list[str]:
    return re.findall(rf"{label}: ([^\s)]+)", t)


def test_every_approved_action_names_its_job_and_mas_job_follows_it_to_the_end_from_the_text_alone(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            # a task: the approval's own text carries the job id and a one-line state
            r = await host.tool("mas_create_task", text="Summarize: the sky is blue.", task_type="summarize", touches=["none"])
            a = text(await owner_yes(host, sc(r)["intent_id"]))
            (tid,) = ids(a, "job id for mas_job")
            assert re.search(r"job id for mas_job: t-\w+ \(task: (running|awaiting_approval|done\w*)\)", a)
            j = await wait_job(host, tid, timeout=60)
            assert "Result" in text(j) or sc(j)["state"].startswith("done")
            # an exam: the same
            r2 = await host.tool("mas_run_admission", environment="llamacpp-local", task_type="text")
            w.script.accuracy[("llamacpp-local", "local-hardware", "text")] = 10
            a2 = text(await owner_yes(host, sc(r2)["intent_id"]))
            (aid,) = ids(a2, "job id for mas_job")
            assert re.search(r"\(admission: (running|done|awaiting_owner)\)", a2) and aid == sc(r2)["job_id"]
            done = await wait_job(host, aid, until=("done", "aborted", "refused", "declined"), timeout=120)
            assert sc(done)["state"] == "done"
        finally:
            await teardown()
    run(go())


def test_every_question_a_chat_relays_shows_the_id_to_answer_with(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            from envrec_coordinator.profile import Profile
            c.services.profile = Profile(tmp_path / "fresh.json", dynamic=c.services.profile.dynamic)
            c.services.profile.apply(c.services.policy)
            r = await host.tool("mas_create_task", text="Summarize this.", task_type="summarize", touches=["none"])
            t = text(r)
            (qid,) = set(ids(t, "intent_id for mas_answer"))
            assert qid.startswith("profile:")
            ch = __import__("envrec_coordinator.harness", fromlist=["CHANNELS"]).CHANNELS["claude-desktop"]
            out = c.harness.decide(ch, qid, "1")  # the id the text showed is the id the answer takes
            assert out["ok"] is True
        finally:
            await teardown()
    run(go())


def test_mas_job_reads_a_run_by_its_id_and_mas_activity_lists_what_is_open(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            r = AgentRun("rr1", "install", nid, "lenovo", "chat")
            r.status = "running"
            c.runs.runs["rr1"] = r
            j = await host.tool("mas_job", job_id="rr1")
            assert sc(j)["ok"] is True and "install run" in text(j) and "running" in text(j) and sc(j)["job_id"] == "rr1"
            c.services._job("t-open", "task", "running", text="x")
            c.services._job("t-shut", "task", "done", text="y")
            act = await host.tool("mas_activity")
            assert "Open or running now:" in text(act) and "task t-open: running" in text(act) and "install run rr1: running" in text(act) and "t-shut" not in text(act)
            assert sc(await host.tool("mas_job", job_id="nope"))["ok"] is False
        finally:
            await teardown()
    run(go())


def test_mas_runs_and_mas_run_read_what_the_coordinator_publishes_without_the_coordinator_state_folder(tmp_path, capsys):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            r = AgentRun("rr2", "install", nid, "lenovo", "chat")
            r.status, r.reason = "running", "waiting for the plan"
            c.runs.runs["rr2"] = r
            c.services._job("t-pub", "task", "running", text="Task t-pub is running.")
            c._publish_runs()
            import argparse

            from envrec_mas import cli
            a = argparse.Namespace(state_dir=str(tmp_path / "c" / "coord"), id="rr2")
            # the coordinator world keeps its state under tmp/c/coordinator
            assert cli.cmd_runs(a) == 0
            out = capsys.readouterr().out
            assert "rr2" in out and "install run" in out and "t-pub" in out and "running" in out
            assert cli.cmd_run(a) == 0
            one = capsys.readouterr().out
            assert '"id": "rr2"' in one and "waiting for the plan" in one
            assert cli.cmd_run(argparse.Namespace(state_dir=str(tmp_path / "c" / "coord"), id="zzz")) == 1
        finally:
            await teardown()
    run(go())


def test_an_exam_prompt_that_times_out_frees_its_place_so_the_next_is_not_turned_away_as_busy(tmp_path, monkeypatch):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            import envrec_coordinator.services as SV
            monkeypatch.setattr(SV, "BUSY_RETRIES", 0)
            w.script.answers.append((lambda e, a, p: e == "llamacpp-local" and "slow-one" in p, lambda e, a, p: (time.sleep(3), "late")[1]))
            w.script.answers.append((lambda e, a, p: e == "llamacpp-local" and "quick-one" in p, "fast"))
            combo = Combo("text", nid, "llamacpp-local", "llama-3.1-8b-instruct-q4_k_m", "local-hardware", None, None)
            svc = c.services
            for t_ in ("t1", "t2", "t3"):
                c.own_task(f"qa-x:{t_}")  # the exam's prompts carry the owner's approval of the exam; here each is owned directly
            r1 = await svc._ask("qa-x", combo, "slow-one a", "t1", -59.0)  # waits one second, then gives up on the call
            r2 = await svc._ask("qa-x", combo, "slow-one b", "t2", -59.0)
            assert r1.timed_out and r2.timed_out
            assert [t.status for t in c.tasks.tasks.values() if t.task_type == "environment_run"].count("cancelled") == 2
            r3 = await svc._ask("qa-x", combo, "quick-one", "t3", 30.0)  # both slow calls are still running in their threads; their places are free
            assert r3.timed_out is False and r3.text == "fast"
        finally:
            await teardown()
    run(go())


def test_a_node_that_is_busy_for_a_moment_is_asked_again_and_not_the_end_of_the_exam(tmp_path, monkeypatch):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            import envrec_coordinator.services as SV
            monkeypatch.setattr(SV, "BUSY_WAIT_S", 0.2)
            calls = {"n": 0}
            real = c.tasks.start_environment_run

            def start(*a, **k):
                calls["n"] += 1
                c.tasks.max_per_node = 0 if calls["n"] <= 2 else 2  # the node is full for the first two asks (the coordinator's own limit speaks first)
                return real(*a, **k)
            c.tasks.start_environment_run = start
            for t_ in ("t1", "t2"):
                c.own_task(f"qa-y:{t_}")
            w.script.answers.append((lambda e, a, p: "quick-two" in p, "fine"))
            combo = Combo("text", nid, "llamacpp-local", "llama-3.1-8b-instruct-q4_k_m", "local-hardware", None, None)
            reply = await c.services._ask("qa-y", combo, "quick-two", "t1", 30.0)
            assert reply.text == "fine" and calls["n"] == 3  # asked three times: twice turned away, the third answered
            monkeypatch.setattr(SV, "BUSY_RETRIES", 1)
            c.tasks.start_environment_run = lambda *a_, **k_: (setattr(c.tasks, "max_per_node", 0), real(*a_, **k_))[1]  # always turned away
            with pytest.raises(RunnerAborted) as exc:
                await c.services._ask("qa-y", combo, "quick-two", "t2", 30.0)
            assert exc.value.reason == "busy"  # and when it never answers, the exam ends as the machine's problem with the reason, nothing counted
        finally:
            await teardown()
    run(go())


def test_a_route_is_dated_and_the_proposal_prints_what_fits_from_the_analysis_alone(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            import json as _json
            import time as _t
            c.runs.analyses.mkdir(parents=True, exist_ok=True)
            facts = {"machine.largest_model_params_b": 8.0, "machine.usable_memory_gib": 7.8, "machine.largest_model_limited_by": "available memory", "analysis.finished": True}
            (c.runs.analyses / f"{nid}.json").write_text(_json.dumps({"run": "a", "node": nid, "stored_at": _t.time() - 3 * 3600, "facts": {k: {"value": v, "status": "tested"} for k, v in facts.items()}}))
            r = await host.tool("mas_explain_route", task_type="summarize", sensitivity="public")
            assert "Facts about lenovo:" in text(r) and ("there is no stored analysis" in text(r) or "its analysis is 3 hours old" in text(r)) and "key names were checked" in text(r)
            c.runs.stored_analysis = lambda node: {"stored_at": _t.time() - 3 * 3600, "facts": {k: {"value": v} for k, v in facts.items()}}
            r2 = await host.tool("mas_explain_route", task_type="summarize", sensitivity="public")
            assert "its analysis is 3 hours old" in text(r2)
            p = await host.tool("mas_propose")
            assert "a model of up to 8.0 billion parameters fits in 7.8 GiB of usable memory (available memory)" in text(p) and "Say nothing more about what fits" in text(p)
        finally:
            await teardown()
    run(go())

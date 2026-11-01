"""The services behind the tools: the entry chat is the channel's, explaining records nothing, a confidential result is withheld from a cloud chat, and the tool door is a closed list. SIMULATED-LOCAL
(real coordinator, real node, real MCP server; the models and the owner's hands are played)."""
from __future__ import annotations

import asyncio
import json

from acceptance_world import build, wait_job
from test_acceptance_run import answers, create, execute, fx, measure, sc, text

from envrec_coordinator.services import Services


def run(coro):
    return asyncio.run(coro)


def test_the_entry_chat_is_the_channels_and_no_argument_can_change_it(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            f = fx("ex3_notes")
            for extra in ({"entry_chat": {"kind": "local", "name": "owner-panel"}}, {"kind": "local"}, {"channel": "panel"}, {"entry": "local"}):
                r = await host.tool("mas_create_task", text=f["input.txt"], task_type="summarize", sensitivity="confidential", **extra)
                assert sc(r)["ok"] is False and sc(r)["refusal"]["code"] == "confidential_from_cloud_chat", extra
            assert not any("entry_chat" in json.dumps(t["inputSchema"]) for t in (await host.rpc("tools/list"))["result"]["tools"]), "no tool offers the entry chat as an argument"
            import inspect
            assert all("entry_chat" not in inspect.signature(getattr(Services, n)).parameters for n in Services.TOOLS)
            assert sc(await host.local.tool("mas_explain_route", task_type="summarize", sensitivity="confidential"))["entry_chat"] == "local"
            assert sc(await host.tool("mas_explain_route", task_type="summarize", sensitivity="confidential"))["entry_chat"] == "cloud"
        finally:
            await teardown()
    run(go())


def test_explaining_a_route_records_no_decision_and_sends_nothing(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            before = len(c.log.events())
            r = await host.tool("mas_explain_route", task_type="summarize", sensitivity="public")
            assert sc(r)["chosen"]
            await asyncio.sleep(0.2)
            assert [e.type for e in c.log.events()[before:]] == [] and not c.log.events("route_decision") and not w.script.calls
        finally:
            await teardown()
    run(go())


def test_a_confidential_result_is_withheld_from_a_cloud_chat_and_shown_to_a_local_one(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            await measure(w, host, "llamacpp-local", "local-hardware", "summarize", 9)
            answers(w, "ex3_notes", "Summarize these client payment notes")
            r = await create(host.local, "ex3_notes", "summarize", "confidential")
            j = await execute(host.local, r)
            assert sc(j)["state"] == "done" and "Total paid" in text(j)
            cloud_view = await host.tool("mas_job", job_id=sc(r)["task_id"])
            assert "Total paid" not in text(cloud_view) and "confidential" in text(cloud_view) and "output" not in sc(cloud_view)
        finally:
            await teardown()
    run(go())


def test_the_tool_door_is_a_closed_list_and_an_unknown_name_is_refused(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            from envrec_coordinator.harness import CHANNELS
            assert c.services.call(CHANNELS["claude-desktop"], "run_shell", {"cmd": "ls"})["ok"] is False
            assert {t["tool"] for t in c.services.specs()} == set(Services.TOOLS)
        finally:
            await teardown()
    run(go())


def test_if_the_facts_change_after_approval_the_task_stops_instead_of_running_somewhere_else(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            await measure(w, host, "llamacpp-local", "local-hardware", "summarize", 9)
            r = await create(host, "ex1_article", "summarize", "public", check=False)
            assert sc(r)["chosen"].startswith("llamacpp-local")
            from acceptance_world import set_server
            set_server(c, nid, up=False)  # between the proposal and the owner's approval the server went away
            await host.tool("mas_request_approval", intent_id=sc(r)["intent_id"])
            j = await wait_job(host, sc(r)["task_id"])
            assert sc(j)["state"] == "route_changed" and "Create the task again" in text(j) and not [x for x in w.script.calls if "article" in x[2]]
        finally:
            await teardown()
    run(go())


def test_the_draft_tool_starts_a_run_refuses_a_correction_without_words_and_has_nothing_to_approve_until_one_is_proposed(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            r = await host.tool("mas_draft", action="correct")
            assert sc(r)["ok"] is False and "own words" in text(r)
            r = await host.tool("mas_draft", action="approve")
            assert sc(r)["ok"] is False and "no proposed draft" in text(r)
            r = await host.tool("mas_draft", action="start")  # no model is configured in this world: the harness says so and drafts nothing
            assert sc(r)["ok"] is False and "no model" in text(r) and sc(r)["run_id"]
            assert (await host.tool("mas_draft", action="nonsense"))["structuredContent"]["ok"] is False
        finally:
            await teardown()
    run(go())

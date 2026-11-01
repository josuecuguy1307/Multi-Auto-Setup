"""The owner's profile and what reads it: nothing starts closed, a task's level is derived (and asked when unclear), the harness proposes what a detected access enables, a run can be cancelled, a status call waits
for a change, and an input is never cut silently. SIMULATED-LOCAL: the real coordinator, node and MCP server of the acceptance world; no test plays a system dialog."""
from __future__ import annotations

import asyncio
import json

import pytest
from acceptance_world import TOUCHES, build
from test_acceptance_run import sc, text

from envrec_coordinator.harness import CHANNELS
from envrec_coordinator.profile import Profile, ProfileError
from envrec_coordinator.router import Policy
from envrec_coordinator.web import wakes_on

CLOUD_ENV = "meta-model-api"
CH = CHANNELS["claude-desktop"]


def run(coro):
    return asyncio.run(coro)


def pending(c, base=""):
    """The harness's own pending chat questions (the profile's, a proposal, a task's level)."""
    return [i for log in c.harness._logs.values() for i in log if i.kind == "ask_choice" and i.extra.get("state") == "pending" and i.extra.get("on_choice") and str(i.extra.get("base", "")).startswith(base)]


def answer(c, ch, intent, option):
    return c.harness.decide(ch, intent.id, option)


def fresh(c, tmp):
    """A profile nobody answered yet, as on a first run."""
    c.services.profile = Profile(tmp / "fresh-profile.json")
    c.services.profile.apply(c.services.policy)


def candidates(r):
    return set(sc(r)["candidates"])


# ---- the profile as data -------------------------------------------------------------------------------------------------------------------------

def test_a_task_level_comes_from_the_profile_and_the_harness_asks_when_it_cannot_tell(tmp_path):
    p = Profile(tmp_path / "p.json")
    p.set("confidential", ["finance", "health"])
    p.set("default_sensitivity", "ask")
    assert p.level_for(["finance"]) == ("confidential", "category:finance")
    assert p.level_for(["health", "source_code"])[0] == "confidential"
    assert p.level_for(["source_code"])[0] == "internal"  # something he did not list: not public either
    assert p.level_for(["none"]) == (None, "ask_each_time") and p.level_for(["unsure"]) == (None, "unclear") and p.level_for(None) == (None, "unclear") and p.level_for([]) == (None, "unclear")
    assert p.level_for(["made-up"]) == (None, "unknown_category")
    p.set("default_sensitivity", "public")
    assert p.level_for(["none"]) == ("public", "owner_default")  # public only because HE chose that default
    with pytest.raises(ProfileError):
        p.set("default_sensitivity", "secret")
    with pytest.raises(ProfileError):
        p.set("cloud_steps", 7)
    with pytest.raises(ProfileError):
        p.set("confidential", ["lunch"])


def test_every_change_is_recorded_with_its_old_value_and_survives_a_restart(tmp_path):
    seen = []
    p = Profile(tmp_path / "p.json", log=lambda t, d: seen.append((t, d)))
    p.set("cloud_spend", "never")
    p.close("environments", "codex")
    again = Profile(tmp_path / "p.json")
    assert again.get("cloud_spend") == "never" and again.closed["environments"] == ["codex"] and "cloud_spend" in again.answered
    assert seen[0][1]["old"] == "<unset>" and seen[0][1]["new"] == "never" and seen[1][1]["new"] == "closed"
    p.close("environments", "codex", False)
    assert Profile(tmp_path / "p.json").closed["environments"] == []  # opening undoes it


# ---- 1. nothing starts closed ---------------------------------------------------------------------------------------------------------------------

def test_a_detected_access_makes_the_environment_available_with_no_closed_default(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            c.services.policy.open_subtypes = Policy.defaults().open_subtypes  # the shipped default, not the world's
            assert c.services.policy.closed_environments == set()
            r = await host.tool("mas_explain_route", task_type="summarize", sensitivity="public")
            assert any(k.startswith(CLOUD_ENV + "|") for k in candidates(r)), (candidates(r), sc(r)["left_out"])  # the key is present on the machine, so the API is a candidate
            assert "closed by the owner" not in json.dumps(sc(r)["left_out"])
        finally:
            await teardown()
    run(go())


def test_closing_is_the_owners_choice_a_chat_question_recorded_and_undone_by_opening(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            c.services.policy.open_subtypes = Policy.defaults().open_subtypes
            r = await host.tool("mas_profile", action="close", what="environment", id=CLOUD_ENV)
            assert sc(r)["ok"] is True
            assert CLOUD_ENV not in c.services.profile.closed["environments"], "asking is not closing: the owner answers first"
            (q,) = pending(c, "profile:close")
            assert [o["id"] for o in q.payload["options"]] == ["close", "keep"]
            answer(c, CH, q, "close")
            r = await host.tool("mas_explain_route", task_type="summarize", sensitivity="public")
            assert not any(k.startswith(CLOUD_ENV + "|") for k in candidates(r)) and sc(r)["left_out"][f"{CLOUD_ENV}@{nid}"] == "closed by the owner"
            assert [e.payload["new"] for e in c.log.events("profile_changed") if e.payload["field"] == "closed:environments"][-1] == "closed"
            await host.tool("mas_profile", action="open", what="environment", id=CLOUD_ENV)
            r = await host.tool("mas_explain_route", task_type="summarize", sensitivity="public")
            assert any(k.startswith(CLOUD_ENV + "|") for k in candidates(r))
        finally:
            await teardown()
    run(go())


def test_the_cloud_stance_and_a_machines_role_leave_cloud_candidates_out_with_a_reason(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            c.services.policy.open_subtypes = Policy.defaults().open_subtypes
            p = c.services.profile
            p.set("cloud_spend", "never"); p.apply(c.services.policy)
            r = await host.tool("mas_explain_route", task_type="summarize", sensitivity="public")
            assert candidates(r) and all(k.startswith(("llamacpp-local", "opencode", "pi")) for k in candidates(r)) and sc(r)["left_out"][f"{CLOUD_ENV}@{nid}"] == "closed by the owner"
            p.set("cloud_spend", "allowed"); p.set_role(nid, "local_only"); p.apply(c.services.policy)
            r = await host.tool("mas_explain_route", task_type="summarize", sensitivity="public")
            assert not any(k.startswith(CLOUD_ENV + "|") for k in candidates(r)) and sc(r)["left_out"][f"{CLOUD_ENV}@{nid}"] == "closed by the owner"
            p.set_role(nid, "not_for_tasks"); p.apply(c.services.policy)
            r = await host.tool("mas_explain_route", task_type="summarize", sensitivity="public")
            assert candidates(r) == set() and set(sc(r)["left_out"].values()) == {"closed by the owner"}
            p.set_role(nid, "general"); p.apply(c.services.policy)
            assert candidates(await host.tool("mas_explain_route", task_type="summarize", sensitivity="public"))
        finally:
            await teardown()
    run(go())


# ---- 3. the questionnaire and the task's level ----------------------------------------------------------------------------------------------------

def test_on_a_first_run_the_harness_asks_the_profile_questions_and_creates_no_task_until_they_are_answered(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            fresh(c, tmp_path)
            r = await host.tool("mas_create_task", text="Summarize: the sky is blue.", task_type="summarize", touches=["none"])
            assert sc(r)["ok"] is False and "what counts as confidential" in text(r).lower() and not c.services.tasks and not c.services.jobs
            seen = []
            for _ in range(30):  # the owner answers each question the harness puts (the first answer, the "that is all" of a list, the first choice of the rest)
                qs = pending(c, "profile:")
                if not qs:
                    break
                q = qs[0]
                seen.append(q.payload["question"].split(":")[1])
                ids = [o["id"] for o in q.payload["options"]]
                pick = "health" if q.payload["question"].startswith("profile:confidential") and "done" not in ids else ("done" if "done" in ids else "allowed" if "allowed" in ids else ids[0])
                answer(c, CH, q, pick)
            p = c.services.profile
            assert p.missing() == [] and p.get("confidential") == ["health"] and nid in p.roles
            assert seen[0] == "confidential" and {"default_sensitivity", "cloud_spend", "cloud_steps", "local_first"} <= set(seen) and any(s == "role" for s in seen)
        finally:
            await teardown()
    run(go())


def test_a_task_that_touches_nothing_is_asked_about_once_and_created_from_the_owners_answer(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            c.services.profile.set("default_sensitivity", "ask")
            r = await host.tool("mas_create_task", text="Summarize: the sky is blue.", task_type="summarize", touches=["none"])
            assert sc(r)["ok"] is True and sc(r)["question"] is True and not c.services.tasks, "asked, not created"
            (q,) = pending(c, "profile:level")
            assert [o["id"] for o in q.payload["options"]] == ["public", "internal", "confidential"]
            out = answer(c, CH, q, "public")
            assert "Task t-" in out["next"] and "(summarize, public)" in out["next"] and len(c.services.tasks) == 1
        finally:
            await teardown()
    run(go())


def test_a_chat_cannot_give_a_level_and_an_unsure_task_is_asked(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            r = await host.tool("mas_create_task", text="x", task_type="summarize", touches=["none"], sensitivity="public")
            assert sc(r)["ok"] is False and "does not give a task's level" in text(r) and not c.services.tasks
            r = await host.tool("mas_create_task", text="x", task_type="summarize", touches=["unsure"])
            assert sc(r)["question"] is True and not c.services.tasks
            r = await host.tool("mas_create_task", text="x", task_type="summarize")
            assert sc(r)["question"] is True  # no statement at all is not "none"
        finally:
            await teardown()
    run(go())


def test_a_cloud_chat_still_cannot_create_a_confidential_task_whatever_the_profile_says(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            r = await host.tool("mas_create_task", text="Pay stub for Ana", task_type="summarize", touches=TOUCHES["confidential"])
            assert sc(r)["ok"] is False and sc(r)["refusal"]["code"] == "confidential_from_cloud_chat"
        finally:
            await teardown()
    run(go())


def test_the_tools_offer_the_catalogs_own_choices_not_a_models_memory(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            specs = {t["name"]: t for t in c.services.specs()}
            assert "sensitivity" not in specs["mas_create_task"]["inputSchema"]["properties"]
            assert specs["mas_create_task"]["inputSchema"]["properties"]["touches"]["items"]["enum"] == [*c.services.profile.categories(), "none", "unsure"]
            assert "meta-model-api:api-key" in specs["mas_add_access"]["inputSchema"]["properties"]["access"]["enum"]
            assert "summarize" in specs["mas_create_task"]["inputSchema"]["properties"]["task_type"]["enum"]
        finally:
            await teardown()
    run(go())


# ---- 2. the harness proposes ----------------------------------------------------------------------------------------------------------------------

def test_the_proposal_comes_from_the_catalog_and_what_is_detected_and_is_one_question(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            c.services.policy.open_subtypes = Policy.defaults().open_subtypes
            r = await host.tool("mas_propose")
            t = text(r)
            assert f"{CLOUD_ENV} via meta-model-api:api-key on" in t and "(cloud)" in t and "summarize" in t and "llamacpp-local via local-hardware" in t
            assert "Not usable yet" in t and "claude-code on lenovo" in t and "automated use not cleared by the owner" in t and "pi on lenovo" in t and "not installed" in t  # what each other environment needs first, from the catalog's typed reasons
            (q,) = pending(c, "proposal:")
            assert [o["id"] for o in q.payload["options"]] == ["ok", "change"]
            assert len(pending(c, "proposal:")) == 1 and "Present this to the owner as it is" in t
            await host.tool("mas_propose")
            assert len(pending(c, "proposal:")) == 1, "the same proposal is the same question"
            out = answer(c, CH, q, "change")
            assert "mas_profile close" in out["next"]
            assert [e.payload["option"] for e in c.log.events("proposal_answered")] == ["change"]
        finally:
            await teardown()
    run(go())


def test_the_proposal_changes_when_the_key_is_gone(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path, key=False)
        try:
            c.services.policy.open_subtypes = Policy.defaults().open_subtypes
            t = text(await host.tool("mas_propose"))
            assert f"{CLOUD_ENV} via" not in t.split("Not usable yet")[0] and "access missing: MODEL_API_KEY" in t  # the whole story, a NAME and never a value
        finally:
            await teardown()
    run(go())


# ---- 7. cancelling a run ---------------------------------------------------------------------------------------------------------------------------

def test_a_run_that_waits_can_be_cancelled_from_the_chat(tmp_path):
    async def go():
        from test_harness_setup import INSTALL, confirm_intent, until, world
        from envrec_coordinator.harness import CHANNELS
        CLAUDE = CHANNELS["claude-desktop"]
        cm, c, agent, nid, r = await world(tmp_path, INSTALL)
        try:
            await until(lambda: confirm_intent(c, CLAUDE, r) is not None)
            out = c.services.call(CLAUDE, "cancel", {"run_id": r.id})
            assert out["ok"] is True and r.status == "cancelled" and r.finished is not None
            assert [e.payload["run"] for e in c.log.events("run_cancelled")] == [r.id]
            await asyncio.sleep(0.2)
            assert r.task.done() and len(c.log.events("setup_step_approved")) == 0
            again = c.services.call(CLAUDE, "cancel", {"run_id": r.id})
            assert again["ok"] is False and "already_cancelled" in again["text"]
            assert c.services.call(CLAUDE, "cancel", {"run_id": "nope"})["ok"] is False
        finally:
            await cm.__aexit__(None, None, None)
    run(go())


# ---- 8. a status call waits for a change ---------------------------------------------------------------------------------------------------------

def out(kinds, waiting=(), finished=False):
    return {"intents": [{"kind": k} for k in kinds], "waiting": list(waiting), "run": {"finished": finished}}


def test_progress_lines_pile_up_and_only_something_that_matters_wakes_a_waiting_status():
    assert wakes_on(out(["progress", "progress"]), "change") is False
    assert wakes_on(out(["progress"]), "") is True  # the old behaviour, still the default of the raw API
    assert wakes_on(out(["progress", "ask_choice"], waiting=["q"]), "change") is True
    assert wakes_on(out(["progress", "text"]), "change") is True and wakes_on(out(["show_data"]), "change") is True
    assert wakes_on(out([], waiting=["q"]), "change") is True and wakes_on(out([], finished=True), "change") is True


def test_the_status_tool_asks_the_core_to_wait_for_a_change_by_default():
    from envrec_harness.mcp_server import McpAdapter

    calls = []

    class Core:
        def intents(self, run_id, since, wait, until=""):
            calls.append((wait, until))
            return {"intents": [], "questions": [], "waiting": [], "run": {"id": run_id, "status": "running", "ended": False, "finished": False}}

    a = McpAdapter(Core(), out=open("/dev/null", "w"))
    a.tool_status({"run_id": "r"})
    a.tool_status({"run_id": "r", "wait_s": 3})
    assert calls == [(20.0, "change"), (3.0, "change")]


# ---- 6. an input is never cut silently --------------------------------------------------------------------------------------------------------------

def test_the_result_says_how_much_text_arrived_and_an_oversize_text_is_refused_with_its_size(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            body = "Tortoises were found on the island. " * 20 + "They will return young tortoises to the wild."
            r = await host.tool("mas_create_task", text=body, task_type="summarize", touches=TOUCHES["public"])
            assert f"Received {len(body)} characters of input, ending with: \"" in text(r) and "return young tortoises to the wild." in text(r)
            big = "x" * 100_001
            r = await host.tool("mas_create_task", text=big, task_type="summarize", touches=TOUCHES["public"])
            assert sc(r)["ok"] is False and "this one is 100001" in text(r) and "nothing was cut" in text(r)
        finally:
            await teardown()
    run(go())

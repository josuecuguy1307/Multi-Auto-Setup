"""Connecting a chat from the owner's side, through the Harness App core: the question, the plan as text, each step approved by a human gesture, the settings file edited and read back, and the undo in a new run.
SIMULATED-LOCAL: a real coordinator, a scripted model, a temporary home and a fake process list: no test touches a real settings file of this computer."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from envrec.model import ScriptedModel
from envrec_coordinator import connect as C
from envrec_coordinator import harness as H
from envrec_coordinator.harness import CHANNELS, Refused
from envrec_harness.intents import for_display as D
from mas_world import coordinator
from test_connect import SERVER, Fake, home, make_env, other_servers_file  # noqa: F401  (the home fixture)
from test_harness_core import Dialog
from test_harness_setup import host_strips, until
from test_mas_agent import AVAILABLE, act

CLAUDE, OWUI, PANEL = CHANNELS["claude-desktop"], CHANNELS["openwebui"], CHANNELS["panel"]
NAMED = [act("detect_hosts"), act("build_connect_plan")] + [act("run_step", args={"step": str(n)}) for n in (1, 2, 3)] + [act("finish_connect", args={"summary": "connected"})]  # the owner named the chat: no question
CONNECT = NAMED + [act("run_step", args={"step": str(n)}) for n in (1, 2, 3)] + [act("finish_connect", args={"summary": "connected"})]
DISCONNECT = [act("load_setup")] + [act("undo_step", args={"step": str(n)}) for n in (3, 2, 1)] + [act("finish_undo", args={"summary": "undone"})]


@pytest.fixture(autouse=True)
def os_dialog_exists(monkeypatch):
    monkeypatch.setattr(H.osconfirm, "available", lambda *a, **k: {"available": True, "mechanism": "fake_dialog"})


def run(coro):
    return asyncio.run(coro)


def script(c, connect=CONNECT, disconnect=DISCONNECT):
    c.runs.model_status = lambda: AVAILABLE
    models = iter([connect, disconnect])
    c.runs._model = lambda: ScriptedModel(list(next(models)))


def pending(r):
    p = r.adapter.pending
    return p if p is not None and p.qid.startswith("approve_") else None


async def approve_all(c, r, ch=CLAUDE, dialog=None):
    """The owner answers every approval of a run in the system dialog (the fake plays it); returns the dialog texts."""
    dialog = dialog or Dialog("approved")
    c.harness.native_confirm = dialog
    since, shown = 0, []
    while not r.task.done():
        for i in c.harness.intents(ch, r.id, since)["intents"]:
            since = max(since, i["seq"])
            if i["kind"] == "confirm" and i.get("state") == "pending":
                n0 = len(dialog.calls)
                b = c.harness.begin_confirmation(ch, i["id"], "os_dialog")
                await until(lambda: c.harness.ticket(ch, b["ticket"])["state"] != "waiting", 20)
                shown.append((i["payload"]["step"], i["payload"]["action"], i["text_fallback"], dialog.calls[n0][1]))
        await asyncio.sleep(0.03)
    await r.task
    return shown


def test_claude_desktop_is_connected_from_the_chat_plan_first_each_step_in_the_system_dialog_and_undone_in_a_new_run(tmp_path, home):
    async def go():
        cfg = home / "Library/Application Support/Claude/claude_desktop_config.json"
        original = other_servers_file(cfg)
        async with coordinator(tmp_path / "c") as c:
            c.connect_env = make_env(tmp_path, home)
            script(c)
            view = c.harness.start(CLAUDE, "connect_host", "claude-desktop")
            r = c.runs.runs[view["id"]]
            assert r.kind == "connect"
            texts = []
            shown = await approve_all(c, r)
            assert r.status == "done", (r.status, r.reason)
            out = c.harness.intents(CLAUDE, r.id)["intents"]
            plan_txt = next(i["text_fallback"] for i in out if "Plan to connect Claude Desktop" in i["text_fallback"])
            assert plan_txt.index("Plan to connect") < plan_txt.index("Step 1 of 3") and "Cmd+Q" in plan_txt and "has not been run on this computer yet" in plan_txt
            assert "SECRET-VALUE-123" not in plan_txt and "API_KEY" not in plan_txt and "<" not in plan_txt and str(home) not in plan_txt
            assert "Step 3 of 3" in plan_txt and r.adapter.plan["plan_hash"] in plan_txt
            assert [(s, a) for s, a, _, _ in shown] == [(1, "run"), (2, "run"), (3, "run")]
            for step, action, text, dialog in shown:
                cmd = D(r.adapter.plan["steps"][step - 1]["exact_command"])
                assert cmd in text and cmd in dialog and host_strips(dialog) == dialog and "<" not in dialog  # the text approved is the plan's command
            assert "Writes to:" in shown[1][3] and "~/Library/Application Support/Claude/claude_desktop_config.json" in shown[1][3]
            doc = json.loads(cfg.read_text())
            assert doc["mcpServers"][SERVER]["args"][-1] == "claude-desktop" and doc["mcpServers"]["other"]["env"]["API_KEY"] == "SECRET-VALUE-123"
            rec = c.runs.stored_setup("connect-claude-desktop")
            assert rec is not None and rec["plan"]["plan_hash"] == r.adapter.plan["plan_hash"] and rec["option_title"] == "Claude Desktop"
            assert c.runs.view(r.id)["actors"]["CLAUDE-CODE"] == 0 and len(c.log.events("owner_authority_refused")) == 0
            ev = [e.payload for e in c.log.events("owner_confirmation")]
            assert len(ev) == 3 and all(p["mechanism"] == "os_dialog" and p["approved"] for p in ev)
            # ---- the undo, in a new run
            view = c.harness.start(CLAUDE, "disconnect_host", "claude-desktop")
            u = c.runs.runs[view["id"]]
            shown_u = await approve_all(c, u)
            assert u.status == "rolled_back", (u.status, u.reason)
            assert [(s, a) for s, a, _, _ in shown_u] == [(3, "undo"), (2, "undo"), (1, "undo")]
            undo_plan = next(i["text_fallback"] for i in c.harness.intents(CLAUDE, u.id)["intents"] if "Plan to undo" in i["text_fallback"])
            assert "Undo step 3 of 3" in undo_plan and "Claude Desktop" in undo_plan
            assert "Removes:" in shown_u[0][3] or "Removes:" in shown_u[1][3]
            assert cfg.read_text() == original and not list(cfg.parent.glob("*.mas-backup-*"))  # exactly as it was
            assert c.runs.stored_setup("connect-claude-desktop") is None
    run(go())


def test_no_chat_door_approves_a_connection_step_and_the_gesture_rule_holds_here_too(tmp_path, home):
    async def go():
        cfg = home / "Library/Application Support/Claude/claude_desktop_config.json"
        original = other_servers_file(cfg)
        async with coordinator(tmp_path / "c", web=True) as c:
            c.connect_env = make_env(tmp_path, home)
            script(c)
            view = c.harness.start(CLAUDE, "connect_host", "claude-desktop")
            r = c.runs.runs[view["id"]]
            await until(lambda: pending(r) is not None)
            qid = pending(r).qid
            assert c.answer_run(r.id, qid, "approve") is False and c.answer_run(r.id, qid, "approve", "owner", "claude-desktop") is False
            i = next(x for x in c.harness.intents(CLAUDE, r.id)["intents"] if x["kind"] == "confirm" and x.get("state") == "pending")
            with pytest.raises(Refused) as exc:
                c.harness.decide(CLAUDE, i["id"], "approve")
            assert exc.value.reason == "confirmation_required"
            assert cfg.read_text() == original and not list(cfg.parent.glob("*.mas-backup-*"))  # nothing ran
            assert c.answer_run(r.id, qid, "approve", "claude-code") is True  # the operator: recorded apart, runs nothing
            await until(lambda: any(e.type == "setup_step" and e.payload.get("state") == "rejected" for e in r.store.events()))
            assert cfg.read_text() == original
            r.task.cancel()
    run(go())


def test_connecting_while_claude_desktop_runs_ends_with_the_reason_and_tells_the_owner_to_quit_it(tmp_path, home):
    async def go():
        (home / "Library/Application Support/Claude").mkdir(parents=True)
        async with coordinator(tmp_path / "c") as c:
            c.connect_env = make_env(tmp_path, home, running=True)
            script(c)
            view = c.harness.start(CLAUDE, "connect_host", "claude-desktop")
            r = c.runs.runs[view["id"]]
            await asyncio.wait_for(r.task, 30)
            assert r.status == "nothing_to_connect"
            facts = {f.key: f.value for f in r.store.current_facts(r.target.id)}
            assert facts["setup.refused"]["reason"] == "app_running" and "Cmd+Q" in facts["setup.refused"]["text"]
            assert not (home / "Library/Application Support/Claude/claude_desktop_config.json").exists()
    run(go())


def test_without_a_named_host_the_owner_is_asked_which_chats_with_only_the_ones_found_on_this_computer(tmp_path, home):
    async def go():
        async with coordinator(tmp_path / "c") as c:
            c.connect_env = make_env(tmp_path, home, binaries=("claude", "codex"))  # no uv, no Claude Desktop folder, no other agent
            script(c, connect=[act("detect_hosts"), act("ask_hosts")])
            view = c.harness.start(CLAUDE, "connect_host", "")
            r = c.runs.runs[view["id"]]
            await until(lambda: r.adapter.pending is not None, 30)
            q = next(i for i in c.harness.intents(CLAUDE, r.id)["intents"] if i["kind"] == "ask_choice")
            assert "Which chats do you want to use Multi Auto Setup in?" in q["text_fallback"] and q["payload"]["writes"] is False
            assert [o["id"] for o in q["payload"]["options"]] == ["claude-code", "codex"]  # only what was found
            assert "Reply with the number or the id" in q["text_fallback"]
            c.harness.decide(CLAUDE, q["id"], "2")
            await until(lambda: r.adapter.host == "codex")
            r.task.cancel()
    run(go())


def test_the_owner_names_the_chat_in_the_core_goal_and_a_goal_without_a_host_to_disconnect_is_refused(tmp_path, home):
    async def go():
        async with coordinator(tmp_path / "c") as c:
            c.connect_env = make_env(tmp_path, home)
            with pytest.raises(Refused) as exc:
                c.harness.start(CLAUDE, "disconnect_host", "")
            assert exc.value.reason == "bad_goal"
            view = c.harness.start(CLAUDE, "disconnect_host", "claude-desktop")  # nothing was connected: a typed refusal, no run of a model
            assert view["status"] == "nothing_to_undo"
    run(go())


def test_a_chat_named_in_the_request_is_the_owners_answer_recorded_from_the_request_and_the_question_is_not_on_the_menu(tmp_path, home):
    async def go():
        async with coordinator(tmp_path / "c") as c:
            c.connect_env = make_env(tmp_path, home)
            script(c, connect=[act("detect_hosts"), act("ask_hosts")])  # a model that still tries the question: it is not offered
            view = c.harness.start(CLAUDE, "connect_host", "claude-desktop")
            r = c.runs.runs[view["id"]]
            await until(lambda: r.adapter.host == "claude-desktop", 30)
            evs = [e.payload for e in r.store.events(type="owner_answer") if e.payload.get("question") == "which_chats"]
            facts = {f.key: f.value for f in r.store.current_facts(r.target.id)}
            assert facts["connect.choice"] == {"host": "claude-desktop", "by": "the owner's request"}
            assert r.adapter.pending is None or not r.adapter.pending.qid.startswith("which_")
            assert not [i for i in c.harness.intents(CLAUDE, r.id)["intents"] if i["kind"] == "ask_choice"]
            assert c.runs.view(r.id)["actors"]["OWNER"] >= 1 and c.runs.view(r.id)["actors"]["CLAUDE-CODE"] == 0
            assert evs == [{"question": "which_chats", "option": "claude-desktop", "from": "the request"}] or evs[0]["from"] == "the request"
            r.task.cancel()
    run(go())


def test_ask_human_is_refused_while_a_dedicated_owner_question_tool_is_on_the_menu_and_the_model_can_recover(tmp_path, home):
    async def go():
        async with coordinator(tmp_path / "c") as c:
            c.connect_env = make_env(tmp_path, home, binaries=("claude", "codex"))
            script(c, connect=[act("detect_hosts"), act(action="ask_human", question="which chat?"), act("ask_hosts")])
            view = c.harness.start(CLAUDE, "connect_host", "")
            r = c.runs.runs[view["id"]]
            await until(lambda: r.adapter.pending is not None, 30)  # the real question came from ask_hosts, after the refusal
            refused = [e.payload for e in r.store.events(type="protocol_refused") if e.payload.get("code") == "owner_question_tool_on_menu"]
            assert len(refused) == 1 and refused[0]["tool"] == "ask_hosts" and refused[0]["action"] == "ask_human"
            assert r.status == "running"
            r.task.cancel()
    run(go())


def test_repeating_ask_human_with_the_owner_question_tool_on_the_menu_still_ends_in_no_progress(tmp_path, home):
    async def go():
        async with coordinator(tmp_path / "c") as c:
            c.connect_env = make_env(tmp_path, home, binaries=("claude", "codex"))
            script(c, connect=[act("detect_hosts")] + [act(action="ask_human", question="which chat?")] * 12)
            view = c.harness.start(CLAUDE, "connect_host", "")
            r = c.runs.runs[view["id"]]
            await asyncio.wait_for(r.task, 60)
            assert r.status == "stopped" and r.summary["stop_reason"] == "no_progress", (r.status, r.summary)
            assert r.adapter.pending is None
    run(go())

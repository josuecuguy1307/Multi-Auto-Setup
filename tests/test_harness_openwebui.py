"""The Open WebUI Pipe (envrec_harness/openwebui_pipe.py) against a real coordinator. SIMULATED-LOCAL: the Function is loaded from its file the way Open WebUI loads it and called with the same
special arguments (`__event_emitter__`, `__event_call__`); the browser's dialogs are played by the test. A real Open WebUI is only used in the supervised verification."""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

import envrec_harness
from envrec.model import ScriptedModel
from envrec_coordinator import harness as H
from mas_world import coordinator
from test_harness_core import HASH, ask_write, owner_events, settle
from test_mas_agent import ANALYSIS_SCRIPT, AVAILABLE, down, node_up

PIPE_FILE = Path(envrec_harness.__file__).parent / "openwebui_pipe.py"


def load_pipe(state_dir):
    spec = importlib.util.spec_from_file_location("owui_function_mas", PIPE_FILE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    p = mod.Pipe()
    p.valves.STATE_DIR = str(state_dir)
    return p


class Chat:
    """The browser: records every status and embed, and answers dialogs from a script."""

    def __init__(self, answers=()):
        self.events, self.dialogs, self.answers = [], [], list(answers)

    async def emit(self, ev):
        self.events.append(ev)

    async def call(self, ev):
        self.dialogs.append(ev)
        a = self.answers.pop(0) if self.answers else False
        return a(ev) if callable(a) else a


def run(coro):
    return asyncio.run(coro)


def scripted(c):
    c.runs.model_status = lambda: AVAILABLE
    c.runs._model = lambda: ScriptedModel(list(ANALYSIS_SCRIPT))


def test_the_function_appears_as_the_model_multi_auto_setup_and_needs_its_valve(tmp_path):
    p = load_pipe(tmp_path)
    assert p.pipes() == [{"id": "mas", "name": "Multi Auto Setup"}]
    p.valves.STATE_DIR = ""
    assert "STATE_DIR" in run(p.pipe({"messages": []}, None, None, None))
    p.valves.STATE_DIR = str(tmp_path)  # a folder with no coordinator in it
    assert "coordinator" in run(p.pipe({"messages": []}, None, None, Chat().call)).lower() and "FileNotFoundError" not in run(p.pipe({"messages": []}, None, None, Chat().call))


def test_without_the_chats_dialogs_it_says_so_and_declares_nothing_so_a_background_call_cannot_erase_what_the_chat_can_do(tmp_path):
    async def go():
        async with coordinator(tmp_path, web=True) as c:
            p = load_pipe(c.state.parent)
            await p.pipe({"messages": []}, None, Chat().emit, Chat().call)  # the chat itself: it declares its dialogs
            assert c.harness.caps["openwebui"]["event_call"] is True
            out = await p.pipe({"messages": []}, None, Chat().emit, None)  # Open WebUI's background call (title, tags): no dialogs
            assert "dialogs" in out and c.harness.caps["openwebui"]["event_call"] is True  # it did not turn the chat's dialogs off
    run(go())


def test_the_owner_picks_analyze_from_the_chats_own_menu_and_sees_progress_cards_and_the_result(tmp_path):
    async def go():
        async with coordinator(tmp_path, web=True) as c:
            agent, task, nid = await node_up(c, tmp_path)
            scripted(c)
            try:
                chat = Chat(["2"])  # 1 = see the network, 2 = analyze the first machine
                out = await load_pipe(c.state.parent).pipe({"messages": [{"role": "user", "content": "analyze the machine"}]}, {}, chat.emit, chat.call)
                menu = chat.dialogs[0]["data"]
                assert chat.dialogs[0]["type"] == "input" and "1. See my network" in menu["message"] and "2. Analyze node1" in menu["message"] and menu["input"]["type"] == "select"
                st = [e["data"]["description"] for e in chat.events if e["type"] == "status"]
                assert any(s.startswith("MODEL:") for s in st) and any(s.startswith("GUARD:") for s in st) and st[-1] == "Done"
                sent = [e["data"] for e in chat.events if e["type"] == "embeds"]
                assert sent and all(d["replace"] is True for d in sent)  # one live set of cards: each change replaces the previous
                last = sent[-1]["embeds"]
                assert any('data-card="network"' in h for h in last) and any('data-card="machine"' in h for h in last) and all("iframe:height" in h for h in last)
                assert len(sent) >= 2 and len({tuple(d["embeds"]) for d in sent}) == len(sent)  # re-emitted only when something changed
                assert "node1" in out and "Windows" in out or "Darwin" in out or "macOS" in out or "machine" in out.lower()
                a = [r for r in c.runs.runs.values()][0]
                assert [e for e in a.store.events() if e.type == "run_requested"][0].payload["via"] == "chat:openwebui"
            finally:
                await down(agent, task)
    run(go())


def test_a_write_is_confirmed_in_the_chats_own_dialog_with_the_plan_and_its_hash(tmp_path):
    async def go():
        async with coordinator(tmp_path, web=True) as c:
            agent, task, nid = await node_up(c, tmp_path)
            scripted(c)
            try:
                holder = {}
                chat = Chat(["2", lambda ev: True])  # analyze, then OK on the confirmation
                orig = c.harness.sync

                def sync_and_ask(run_):
                    if "p" not in holder:  # a protocol (T3a's) asks for a write inside this run
                        holder["p"] = c.harness.request_confirmation(run_.id, summary="Install llama.cpp", plan_hash=HASH, command="curl -L X | tar x", undo="rm -r ~/mas/llama")
                    return orig(run_)
                c.harness.sync = sync_and_ask
                out = await load_pipe(c.state.parent).pipe({"messages": []}, {}, chat.emit, chat.call)
                conf = [d for d in chat.dialogs if d["type"] == "confirmation"][0]["data"]
                assert HASH in conf["message"] and "curl -L X | tar x" in conf["message"] and "rm -r ~/mas/llama" in conf["message"]
                assert holder["p"].future.result() is True and "approved" in out
                ev = owner_events(c, "owner_confirmation")[0]
                assert ev.tag == "OWNER" and ev.payload["channel"] == "openwebui" and ev.payload["mechanism"] == "host_dialog" and ev.payload["plan_hash"] == HASH
            finally:
                await down(agent, task)
    run(go())


def test_cancelling_the_chats_dialog_approves_nothing(tmp_path):
    async def go():
        async with coordinator(tmp_path, web=True) as c:
            agent, task, nid = await node_up(c, tmp_path)
            scripted(c)
            try:
                holder = {}
                chat = Chat(["2", False])
                orig = c.harness.sync

                def sync_and_ask(r):
                    if "p" not in holder:
                        holder["p"] = c.harness.request_confirmation(r.id, summary="s", plan_hash=HASH, command="c", undo="u")
                    return orig(r)
                c.harness.sync = sync_and_ask
                out = await load_pipe(c.state.parent).pipe({"messages": []}, {}, chat.emit, chat.call)
                assert holder["p"].future.result() is False and "declined" in out
                assert owner_events(c, "owner_confirmation")[0].payload["approved"] is False
            finally:
                await down(agent, task)
    run(go())

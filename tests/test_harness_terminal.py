"""A terminal agent (Claude Code, Codex CLI, Gemini CLI, opencode...) as a host of the Harness App. It connects through the same stdio adapter as Claude Desktop, on its own channel (`terminal`, or `terminal-local` when its
model runs on this computer): a model decides what it calls, so it is never the owner; it shows tool results as text; a write is confirmed by an elicitation form when its handshake declares one, and otherwise by the
operating system's dialog. SIMULATED-LOCAL: the agent is played by the test over the real adapter subprocess; a real terminal agent was NOT run (UNTESTED)."""
from __future__ import annotations

import asyncio

import pytest

from envrec_coordinator import harness as H
from envrec_coordinator.harness import CHANNELS, Refused
from mas_world import coordinator
from test_harness_core import Dialog
from test_harness_mcp import FakeHost
from test_harness_setup import tree, until, world
from test_mas_agent import act, down, node_up
from test_harness_openwebui import scripted

TERMINAL, LOCAL = CHANNELS["terminal"], CHANNELS["terminal-local"]


@pytest.fixture(autouse=True)
def os_dialog_exists(monkeypatch):
    monkeypatch.setattr(H.osconfirm, "available", lambda *a, **k: {"available": True, "mechanism": "fake_dialog"})


def run(coro):
    return asyncio.run(coro)


def test_a_terminal_agent_is_a_channel_with_a_model_in_the_middle_that_lists_no_confirmation_a_model_could_press():
    for ch, cloud in ((TERMINAL, True), (LOCAL, False)):
        assert ch.model_in_middle is True and ch.cloud is cloud
        assert "panel_click" not in ch.mechanisms and "host_dialog" not in ch.mechanisms  # HA2: nothing a model could press
        assert ch.mechanisms == ("host_elicitation", "os_dialog")


def test_a_terminal_agent_reads_the_network_as_text_and_its_handshake_is_kept_as_evidence(tmp_path):
    async def go():
        async with coordinator(tmp_path, web=True) as c:
            agent, task, nid = await node_up(c, tmp_path)
            host = FakeHost(c, tmp_path, caps={}, channel="terminal", client="fake-terminal-agent")
            try:
                await host.start()
                res = await host.tool("mas_network")
                text = res["content"][0]["text"]
                assert "node1 — paired machine" in text and "Coordinator" in text and "<svg" not in text  # words only: a terminal shows no cards
                assert c.harness.capabilities(TERMINAL)["confirmations"] == ["os_dialog"]  # no elicitation declared, so the system dialog
                assert c.harness.caps["terminal"]["elicitation"] is False
                assert (c.harness.dir / "handshake-terminal-fake-terminal-agent.json").exists() or any(c.harness.dir.glob("handshake-terminal*"))
                assert (c.harness.dir / "terminal.token").exists() and (c.harness.dir / "terminal-local.token").exists()  # a token of its own per channel
            finally:
                await host.stop()
                await down(agent, task)
    run(go())


def test_a_terminal_agent_prepares_a_machine_and_only_the_system_dialog_approves_a_step(tmp_path):
    async def go():
        cm, c, agent, nid, _ = await world(tmp_path, [act("load_context"), act("recheck_fit"), act("build_plan"), act("run_step", args={"step": "1"})], web=True, start=False)
        host = FakeHost(c, tmp_path, caps={}, channel="terminal", client="fake-terminal-agent")
        try:
            await host.start()
            dialog = Dialog("approved")
            c.harness.native_confirm = dialog
            res = await host.tool("mas_prepare", node="node1")
            run_id = res["structuredContent"]["run_id"]
            conf = None
            for _ in range(40):
                conf = next((i for i in res["structuredContent"]["intents"] if i["kind"] == "confirm"), None)
                if conf:
                    break
                res = await host.tool("mas_status", run_id=run_id, wait_s=2)
            assert conf["kind"] == "confirm" and conf["actor"] == "GUARD" and conf["id"].startswith(f"{run_id}:approve:approve_run_1")  # the first step of this run
            refused = await host.tool("mas_answer", intent_id=conf["id"], option="approve")  # the agent's model tries to approve with the answer tool
            assert refused["isError"] is True and "confirmation_required" in refused["content"][0]["text"]
            assert len(c.log.events("setup_step_approved")) == 0 and tree(agent.state.dir / "mas") == []
            out = await host.tool("mas_request_approval", intent_id=conf["id"])
            assert "system dialog" in out["content"][0]["text"] and out["structuredContent"]["state"] == "approved"
            await until(lambda: len(c.log.events("setup_step_approved")) == 1)
            ev = [e.payload for e in c.log.events("owner_confirmation")]
            assert len(ev) == 1 and ev[0]["channel"] == "terminal" and ev[0]["mechanism"] == "os_dialog" and len(dialog.calls) == 1
            assert "Run step 1 of 6" in dialog.calls[0][1] and "MAS_HOME" in dialog.calls[0][1] and "<" not in dialog.calls[0][1]
        finally:
            await host.stop()
            await cm.__aexit__(None, None, None)
    run(go())


def test_a_terminal_agent_that_declares_an_elicitation_form_is_asked_there_and_one_with_a_local_model_sees_what_a_cloud_one_does_not(tmp_path):
    async def go():
        async with coordinator(tmp_path, web=True) as c:
            agent, task, nid = await node_up(c, tmp_path)
            scripted(c)
            try:
                host = FakeHost(c, tmp_path, caps={"elicitation": {"form": {}}}, channel="terminal", client="fake-terminal-agent")
                await host.start()
                assert c.harness.capabilities(TERMINAL)["confirmations"] == ["host_elicitation", "os_dialog"]
                await host.stop()
                # sensitivity: a confidential run is withheld from the provider-model channel and shown to the local-model one
                view = c.harness.start(CHANNELS["openwebui"], "analyze", nid, "confidential")
                r = c.runs.runs[view["id"]]
                await r.task
                cloud = [i["text_fallback"] for i in c.harness.intents(TERMINAL, r.id)["intents"]]
                local = [i["text_fallback"] for i in c.harness.intents(LOCAL, r.id)["intents"]]
                assert cloud and all("Withheld" in x for x in cloud) and any("node1" in x for x in local)
                with pytest.raises(Refused) as exc:
                    c.harness.start(TERMINAL, "analyze", nid, "confidential")  # a provider-model agent cannot create a confidential run
                assert exc.value.reason == "cloud_cannot_create_confidential"
                c.harness.start(LOCAL, "analyze", nid, "confidential")  # one whose model is local may
            finally:
                await down(agent, task)
    run(go())

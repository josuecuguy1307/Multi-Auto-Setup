"""The Claude Desktop adapter (envrec_harness.mcp_server) against a real coordinator. SIMULATED-LOCAL: the adapter is a real subprocess speaking MCP over stdio; the host (Claude Desktop) is played by
the test, which answers elicitations the way a host's UI would. A real Claude Desktop is only used in the supervised verification."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

import envrec_harness
from envrec_harness import mcp_server as M
from envrec_coordinator import harness as H
from envrec_coordinator.harness import CHANNELS
from mas_world import coordinator
from test_harness_core import HASH, Dialog, ask_write, owner_events, settle  # noqa: F401  (the os_dialog_exists fixture applies here too)

SRC = str(Path(envrec_harness.__file__).parents[1])
CLAUDE = CHANNELS["claude-desktop"]


class FakeHost:
    """What Claude Desktop is to the adapter: it starts it, says what it supports, calls tools, and answers the server's own requests (elicitation) from its 'UI'."""

    def __init__(self, c, tmp, *, caps, channel="claude-desktop", client="fake-claude-desktop"):
        self.c, self.tmp, self.caps, self.n, self.channel, self.client = c, tmp, caps, 0, channel, client
        self.elicitations: list[dict] = []
        self.on_elicit = lambda params: {"action": "accept", "content": {"approve": True}}
        self.waiters: dict = {}

    async def start(self):
        self.proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "envrec_harness.mcp_server", "--state-dir", str(self.c.state.parent), "--channel", self.channel, env={**os.environ, "PYTHONPATH": SRC},
                                                         stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        self.reader = asyncio.create_task(self._read())
        init = await self.rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": self.caps, "clientInfo": {"name": self.client, "version": "0"}})
        self.write({"jsonrpc": "2.0", "method": "notifications/initialized"})
        await settle(lambda: self.c.harness.caps.get(self.channel) is not None)
        return init

    def write(self, msg):
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())

    async def _read(self):
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                return
            m = json.loads(line)
            if "method" in m and "id" in m:  # the server asks the host: elicitation
                self.elicitations.append(m["params"])
                assert m["method"] == "elicitation/create"
                self.write({"jsonrpc": "2.0", "id": m["id"], "result": self.on_elicit(m["params"])})
            elif "id" in m and m["id"] in self.waiters:
                self.waiters.pop(m["id"]).set_result(m)

    async def rpc(self, method, params=None, timeout=30):
        self.n += 1
        fut = asyncio.get_running_loop().create_future()
        self.waiters[self.n] = fut
        self.write({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}})
        return await asyncio.wait_for(fut, timeout)

    async def tool(self, name, **args):
        m = await self.rpc("tools/call", {"name": name, "arguments": args})
        return m["result"]

    async def stop(self):
        self.proc.stdin.close()
        await asyncio.wait_for(self.proc.wait(), 10)
        self.reader.cancel()


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def host_for(tmp_path):
    async def make(c, **caps):
        h = FakeHost(c, tmp_path, caps=caps)
        await h.start()
        return h
    return make


def test_the_handshake_is_read_from_the_host_and_reported_to_the_core_as_evidence(tmp_path):
    async def go():
        async with coordinator(tmp_path, web=True) as c:
            h = FakeHost(c, tmp_path, caps={"elicitation": {"form": {}}, "extensions": {"io.modelcontextprotocol/ui": {"mimeTypes": ["text/html;profile=mcp-app"]}}})
            init = await h.start()
            assert init["result"]["protocolVersion"] == "2025-11-25" and init["result"]["serverInfo"]["name"] == "multi-auto-setup"
            assert c.harness.caps["claude-desktop"]["elicitation"] is True and c.harness.caps["claude-desktop"]["mcp_apps"] is True
            ev = json.loads((c.harness.dir / "handshake-claude-desktop-fake-claude-desktop.json").read_text())  # one file per client: two Claude clients never overwrite each other's evidence
            assert ev["handshake"]["client"]["name"] == "fake-claude-desktop" and ev["declared"]["elicitation"] is True
            tools = (await h.rpc("tools/list"))["result"]["tools"]
            seen_by_model = [t for t in tools if "visibility" not in t["_meta"].get("ui", {})]
            assert sorted(t["name"] for t in seen_by_model) == sorted(["mas_activity", "mas_answer", "mas_connect", "mas_disconnect", "mas_network", "mas_pair", "mas_prepare", "mas_request_approval", "mas_start", "mas_status", "mas_undo", "mas_unpair",
                                                                         "mas_add_access", "mas_cancel", "mas_create_task", "mas_explain_route", "mas_profile", "mas_propose", "mas_job", "mas_remove_access", "mas_run_admission", "mas_board_start", "mas_board_show", "mas_board_approve", "mas_measurements", "mas_draft"])  # twelve entry tools (pair and unpair added ON PURPOSE, each ending in the owner's confirmation) and the eleven the core publishes (its services): nothing to orchestrate
            assert all(t["_meta"]["ui"]["resourceUri"] == "ui://mas/cards" for t in seen_by_model if t["name"] in M.TOOLS)  # the entry tools show cards; the core's services return text that stands alone (cards never render in this host)
            assert all(t["_meta"] == {} for t in seen_by_model if t["name"] not in M.TOOLS)
            assert sorted(t["name"] for t in tools if t["_meta"].get("ui", {}).get("visibility") == ["app"]) == ["mas_live", "mas_view"]  # the view's own tools are hidden from the model
            res = (await h.rpc("resources/read", {"uri": "ui://mas/cards"}))["result"]["contents"][0]
            assert res["mimeType"] == "text/html;profile=mcp-app" and "ui/initialize" in res["text"] and "default-src 'none'" in res["text"]
            await h.stop()
    run(go())


def test_a_host_without_elicitation_is_told_so_and_the_core_offers_the_system_dialog_instead(tmp_path):
    async def go():
        async with coordinator(tmp_path, web=True) as c:
            h = FakeHost(c, tmp_path, caps={})
            await h.start()
            assert c.harness.caps["claude-desktop"]["elicitation"] is False and c.harness.mechanisms(CLAUDE) == ["os_dialog"]
            await h.stop()
    run(go())


def test_the_network_tool_returns_the_cloud_safe_text_and_a_card_for_the_apps_view(tmp_path):
    async def go():
        from test_mas_agent import node_up, down
        async with coordinator(tmp_path, web=True) as c:
            agent, task, nid = await node_up(c, tmp_path)
            h = FakeHost(c, tmp_path, caps={})
            await h.start()
            try:
                r = await h.tool("mas_network")
                assert "node1" in r["content"][0]["text"] and 'data-card="network"' in r["_meta"]["mas/cards"][0]["html"]
                assert "data-card" not in json.dumps(r["structuredContent"]) and "data-card" not in r["content"][0]["text"]  # the card is for the view, never in what the chat's model reads
                assert "fingerprint" not in json.dumps(r) and not r.get("isError")
                assert owner_events(c, "cloud_egress")  # what left for a cloud chat is on record
            finally:
                await h.stop()
                await down(agent, task)
    run(go())


async def pending_write(c, h):
    p = ask_write(c)
    return p


def test_a_model_calling_the_answer_tool_on_a_write_gets_a_refusal_and_nothing_is_approved(tmp_path):
    async def go():
        async with coordinator(tmp_path, web=True) as c:
            h = FakeHost(c, tmp_path, caps={"elicitation": {}})
            await h.start()
            p = ask_write(c)
            r = await h.tool("mas_answer", intent_id=p.intent.id, option="approve")
            assert r["isError"] is True and "confirmation_required" in r["content"][0]["text"]
            assert not p.future.done() and h.elicitations == [] and owner_events(c, "owner_confirmation") == []
            await h.stop()
    run(go())


def test_the_approval_tool_obtains_the_owners_answer_from_the_hosts_own_elicitation_for_the_exact_plan(tmp_path):
    async def go():
        async with coordinator(tmp_path, web=True) as c:
            h = FakeHost(c, tmp_path, caps={"elicitation": {"form": {}}})
            await h.start()
            p = ask_write(c)
            r = await h.tool("mas_request_approval", intent_id=p.intent.id)
            el = h.elicitations[0]
            assert HASH in el["message"] and "curl -L X | tar x" in el["message"] and "rm -r ~/mas/llama" in el["message"]  # the text is the core's, the model wrote none of it
            assert el["requestedSchema"]["properties"]["approve"]["type"] == "boolean"
            assert r["structuredContent"]["state"] == "approved" and not r.get("isError") and await asyncio.wait_for(p.future, 2) is True
            ev = owner_events(c, "owner_confirmation")[0]
            assert ev.tag == "OWNER" and ev.payload["mechanism"] == "host_elicitation" and ev.payload["channel"] == "claude-desktop" and ev.payload["plan_hash"] == HASH
            await h.stop()
    run(go())


@pytest.mark.parametrize("reply,expected", [({"action": "decline"}, "declined"), ({"action": "cancel"}, None), ({"action": "accept", "content": {"approve": False}}, "declined")])
def test_a_declined_dismissed_or_unticked_elicitation_approves_nothing(tmp_path, reply, expected):
    async def go():
        async with coordinator(tmp_path, web=True) as c:
            h = FakeHost(c, tmp_path, caps={"elicitation": {}})
            h.on_elicit = lambda params: reply
            await h.start()
            p = ask_write(c)
            r = await h.tool("mas_request_approval", intent_id=p.intent.id)
            assert r["structuredContent"].get("state") == expected
            assert not (p.future.done() and p.future.result() is True) and not [e for e in owner_events(c, "owner_confirmation") if e.payload.get("approved")]
            await h.stop()
    run(go())


def test_an_elicitation_answered_after_the_plan_changed_is_refused_by_the_core(tmp_path):
    async def go():
        async with coordinator(tmp_path, web=True) as c:
            h = FakeHost(c, tmp_path, caps={"elicitation": {}})
            await h.start()
            p = ask_write(c)

            def reply(params):
                c.harness.change_plan(p.intent.id, "sha256:" + "99" * 32)  # the plan moves while the owner is looking at the old one
                return {"action": "accept", "content": {"approve": True}}
            h.on_elicit = reply
            r = await h.tool("mas_request_approval", intent_id=p.intent.id)
            assert r["isError"] is True and "plan_hash_mismatch" in r["content"][0]["text"] and not p.future.done() and owner_events(c, "owner_confirmation") == []
            await h.stop()
    run(go())


def test_without_elicitation_the_approval_tool_goes_through_the_system_dialog_and_never_through_the_model(tmp_path):
    async def go():
        async with coordinator(tmp_path, web=True) as c:
            dlg = Dialog("approved")
            c.harness.native_confirm = dlg
            h = FakeHost(c, tmp_path, caps={})
            await h.start()
            p = ask_write(c)
            r = await h.tool("mas_request_approval", intent_id=p.intent.id)
            assert r["structuredContent"]["state"] == "approved" and r["structuredContent"]["mechanism"] == "os_dialog" and h.elicitations == [] and HASH in dlg.calls[0][1]
            assert owner_events(c, "owner_confirmation")[0].payload["mechanism"] == "os_dialog"
            await h.stop()
    run(go())


def test_with_nothing_available_the_tool_says_the_owner_panel_is_the_way(tmp_path, monkeypatch):
    async def go():
        monkeypatch.setattr(H.osconfirm, "available", lambda *a, **k: {"available": False, "mechanism": None})
        async with coordinator(tmp_path, web=True) as c:
            h = FakeHost(c, tmp_path, caps={})
            await h.start()
            p = ask_write(c)
            r = await h.tool("mas_request_approval", intent_id=p.intent.id)
            assert r["structuredContent"]["state"] == "unavailable" and "panel" in r["content"][0]["text"] and not p.future.done()
            await h.stop()
    run(go())


def test_a_host_that_did_not_declare_elicitation_is_never_asked_for_it_even_if_the_core_lists_it(tmp_path):
    async def go():
        async with coordinator(tmp_path, web=True) as c:
            dlg = Dialog("approved")
            c.harness.native_confirm = dlg
            h = FakeHost(c, tmp_path, caps={})
            await h.start()
            c.harness.declare(CLAUDE, {"elicitation": True})  # another Claude client on the same channel declared it
            p = ask_write(c)
            r = await h.tool("mas_request_approval", intent_id=p.intent.id)
            assert h.elicitations == [] and r["structuredContent"]["mechanism"] == "os_dialog" and r["structuredContent"]["state"] == "approved"
            await h.stop()
    run(go())

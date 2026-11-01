"""When a node types the right code, the HARNESS raises the owner's confirmation in the operating system's own dialog. Found in the first real pairing from Claude Desktop: the code was typed, the node said
"waiting for the owner to confirm", and no dialog ever appeared, because nothing raised one unless the chat's model asked again and then passed an id it had not been shown. The tests before played the
owner's dialog; these play only the operating system: the fake dialog records the request and holds until the test lets it go, and everything is asserted BEFORE it answers. SIMULATED-LOCAL (the real
dialog is shown only in a supervised run)."""
from __future__ import annotations

import asyncio
import io
import json

import pytest

from envrec_coordinator import harness as H
from envrec_coordinator.harness import CHANNELS, Refused
from envrec_harness import mcp_server
from envrec_harness.client import CoreClient
from mas_world import coordinator, events
from test_harness_core import Dialog
from test_harness_pairing import FakeNotice, os_dialog_exists, run, text_of, wait_for, waiting_node  # noqa: F401  (the fixture is used by name)

CLAUDE = CHANNELS["claude-desktop"]


def adapter_for(c):
    return mcp_server.McpAdapter(CoreClient(c.harness.dir, "claude-desktop"), out=io.StringIO())


async def code_typed(c, tmp_path, name="lenovo"):
    """A computer waits, the chat asks for its code (ONE tool call), the code is typed on it. Returns (node task, adapter, the code)."""
    adapter = adapter_for(c)
    task, box, w = await waiting_node(c, tmp_path, name)
    await asyncio.to_thread(adapter.tools_call, {"name": "mas_pair", "arguments": {"machine": name}})
    code = c.owner.view()["code"]
    box.set_result(code)
    return task, adapter, code


def test_the_harness_raises_the_dialog_by_itself_and_the_chat_cannot_complete_the_pairing_without_it(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c", answer=None, discovery=True, web=True) as c:
            dialog = Dialog("approved", block=True)  # the operating system: it records the request and holds
            c.harness.native_confirm = dialog
            task, adapter, code = await code_typed(c, tmp_path)
            await wait_for(lambda: dialog.opened.is_set())  # NO further tool call was made: the harness asked by itself
            title, message = dialog.calls[0]
            cand = c.owner.view()["candidate"]
            assert "lenovo" in message and cand["fingerprint"][:16] in message and code not in message  # the owner sees which computer and its key; the code is already used up
            assert [e.payload["offer"] for e in events(c, "pairing_dialog_raised")] and events(c, "pairing_dialog_raised")[0].payload["tag"] == "GUARD"
            assert not events(c, "owner_confirmation") and c.registry.all() == []  # nothing is paired by raising a dialog
            intent = next(p.intent for p in c.harness._pending.values())
            with pytest.raises(Refused) as e1:  # the chat's model cannot answer it as a choice
                c.harness.decide(CLAUDE, intent.id, "approve")
            assert e1.value.reason == "confirmation_required"
            joined = c.harness.begin_confirmation(CLAUDE, intent.id, "os_dialog")  # asking again only joins the open dialog: no second window, no answer from the chat
            assert joined["state"] == "waiting" and len(dialog.calls) == 1
            tk = next(iter(c.harness._tickets.values()))
            with pytest.raises(Refused) as e3:  # nor complete the operating system's ticket as if a host had answered
                c.harness.complete_confirmation(CLAUDE, tk.id, tk.plan_hash, "accept")
            assert e3.value.reason == "not_host_answered"
            assert c.registry.all() == []
            dialog.release.set()  # now the owner answers
            await asyncio.wait_for(task, 10)
            assert [n.name for n in c.registry.all()] == ["lenovo"]
            conf = events(c, "owner_confirmation")
            assert len(conf) == 1 and conf[0].payload["mechanism"] == "os_dialog" and conf[0].payload["approved"] is True
    run(go())


def test_the_chat_asking_again_is_told_the_dialog_is_open_and_has_nothing_to_call(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c", answer=None, discovery=True, web=True) as c:
            dialog = Dialog("approved", block=True)
            c.harness.native_confirm = dialog
            task, adapter, code = await code_typed(c, tmp_path)
            await wait_for(lambda: dialog.opened.is_set())
            out = c.harness.pairing(CLAUDE)
            assert "confirmation dialog is now open" in text_of(out) and out["intents"][0]["kind"] == "text" and "intent_id" not in text_of(out)
            r = await asyncio.to_thread(adapter.tools_call, {"name": "mas_pair", "arguments": {}})
            assert "confirmation dialog is now open" in r["content"][0]["text"] and "lenovo" in r["content"][0]["text"]
            dialog.release.set()
            await asyncio.wait_for(task, 10)
    run(go())


def test_a_dialog_the_owner_declines_pairs_nothing(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c", answer=None, discovery=True, web=True) as c:
            dialog = Dialog("declined", block=True)
            c.harness.native_confirm = dialog
            task, adapter, code = await code_typed(c, tmp_path)
            await wait_for(lambda: dialog.opened.is_set())
            dialog.release.set()
            with pytest.raises(Exception):
                await asyncio.wait_for(task, 10)  # the node is told it was refused
            assert c.registry.all() == [] and events(c, "owner_confirmation")[0].payload["approved"] is False
    run(go())


def test_an_offer_that_did_not_come_from_a_chat_raises_no_dialog(tmp_path):
    """The panel and `mas pair` have their own front ends; the harness raises a dialog only for what a chat asked for."""
    async def go():
        async with coordinator(tmp_path / "c", answer=None, discovery=True, web=True) as c:
            dialog = Dialog("approved")
            c.harness.native_confirm = dialog
            task, box, w = await waiting_node(c, tmp_path, "lenovo")
            c.offer_for_waiting(w.id, via="web")
            box.set_result(c.owner.view()["code"])
            await wait_for(lambda: c.owner.view()["status"] == "confirm")
            await asyncio.sleep(0.5)
            assert dialog.calls == [] and not events(c, "pairing_dialog_raised")
            c.decide_pairing(c.owner.view()["offer"], True, "web")
            await asyncio.wait_for(task, 10)
    run(go())


def test_when_no_dialog_can_be_raised_the_confirmation_waits_and_the_chat_is_shown_its_id(tmp_path, monkeypatch):
    async def go():
        async with coordinator(tmp_path / "c", answer=None, discovery=True, web=True) as c:
            monkeypatch.setattr(H.osconfirm, "available", lambda *a, **k: {"available": False, "mechanism": None})
            task, adapter, code = await code_typed(c, tmp_path)
            await wait_for(lambda: c.owner.view()["status"] == "confirm")
            assert [e.payload["reason"] for e in events(c, "pairing_dialog_not_raised")] == ["os_dialog_unavailable"]
            r = await asyncio.to_thread(adapter.tools_call, {"name": "mas_pair", "arguments": {}})
            intent_id = r["structuredContent"]["intents"][0]["id"]
            assert f"intent_id for mas_request_approval: {intent_id}" in r["content"][0]["text"]  # the id is in the words the chat reads, not only in structured data
            c.decide_pairing(c.owner.view()["offer"], False, "web")
            with pytest.raises(Exception):
                await asyncio.wait_for(task, 10)
    run(go())


def test_every_pending_confirmation_a_chat_can_be_asked_to_approve_carries_its_id_in_the_text(tmp_path):
    """The class behind the real failure: a model must never be asked to pass an id it was not shown. Whatever creates a confirmation, the text its chat reads has the id."""
    async def go():
        async with coordinator(tmp_path / "c", answer=None, discovery=True, web=True) as c:
            adapter = adapter_for(c)
            await pair_one(c, tmp_path)
            un = await asyncio.to_thread(adapter.tools_call, {"name": "mas_unpair", "arguments": {"machine": "lenovo"}})
            plain = c.harness.request_confirmation(None, summary="Do a thing", plan_hash="sha256:" + "ab" * 32, command="x", undo="y")
            for pending in c.harness._pending.values():
                shown = c.harness.for_channel(pending.intent, CLAUDE)
                assert f"intent_id for mas_request_approval: {pending.intent.id}" in shown["text_fallback"], pending.intent.payload["summary"]
            assert f"intent_id for mas_request_approval: {un['structuredContent']['intents'][0]['id']}" in un["content"][0]["text"]
            assert plain.intent.id in c.harness.for_channel(plain.intent, CLAUDE)["text_fallback"]
            assert "intent_id" not in c.harness.for_channel(plain.intent, CHANNELS["cli"])["text_fallback"]  # the owner's own terminal types yes: it is not told to call a tool
    run(go())


async def pair_one(c, tmp_path):
    dialog = Dialog("approved")
    c.harness.native_confirm = dialog
    task, adapter, code = await code_typed(c, tmp_path)
    await asyncio.wait_for(task, 10)
    assert [n.name for n in c.registry.all()] == ["lenovo"]


def test_asking_to_approve_an_id_that_does_not_exist_says_what_is_pending(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c", answer=None, discovery=True, web=True) as c:
            held = c.harness.request_confirmation(None, summary="Do a thing", plan_hash="sha256:" + "cd" * 32, command="x", undo="y")
            with pytest.raises(Refused) as e:
                c.harness.begin_confirmation(CLAUDE, "no-such-intent", "os_dialog")
            assert e.value.reason == "nothing_to_confirm" and held.intent.id in e.value.message and "Do a thing" in e.value.message
            c.harness._pending.clear()
            with pytest.raises(Refused) as e2:
                c.harness.begin_confirmation(CLAUDE, "no-such-intent", "os_dialog")
            assert "none" in e2.value.message and "already open" in e2.value.message
    run(go())

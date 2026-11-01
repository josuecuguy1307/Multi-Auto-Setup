"""Pairing and unpairing from a chat (`mas_pair`, `mas_unpair`): thin tools over what the coordinator already does, and above all WHO can complete them. The chat can ask for a code and read the state; a
pairing or an unpair is completed only by a confirmation the owner answers with a gesture of their own, and nothing a model calls can stand in for it.
SIMULATED-LOCAL: a real coordinator and real nodes on loopback, a fake for the operating system's dialog that a test controls (the real dialog is shown only in a supervised run)."""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from envrec_coordinator import harness as H
from envrec_coordinator.harness import CHANNELS, Refused
from envrec_harness import mcp_server
from envrec_harness.client import CoreClient
from envrec_node.join import join
from mas_world import coordinator, events, pair_node
from test_harness_core import Dialog

CLAUDE, PANEL = CHANNELS["claude-desktop"], CHANNELS["panel"]


class FakeNotice:
    """The dialog that shows the pairing code on the owner's screen, played by a test: it remembers what it was told to show and whether it is still open."""

    shown: list = []

    def __init__(self, title, message):
        self.title, self.message, self.open = title, message, True

    def alive(self):
        return self.open

    def close(self):
        self.open = False


@pytest.fixture(autouse=True)
def os_dialog_exists(monkeypatch):
    monkeypatch.setattr(H.osconfirm, "available", lambda *a, **k: {"available": True, "mechanism": "fake_dialog"})
    FakeNotice.shown = []

    def notice(title, message, **kw):
        n = FakeNotice(title, message)
        FakeNotice.shown.append(n)
        return n
    monkeypatch.setattr(H.osconfirm, "native_notice", notice)  # before the coordinator is built: the core takes it at start


def run(coro):
    return asyncio.run(coro)


async def wait_for(pred, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        await asyncio.sleep(0.05)
    raise AssertionError("never happened")


async def waiting_node(c, tmp, name):
    """A computer that ran `mas join`: it waits for the owner's code; `box` is what the owner types on it."""
    box = asyncio.get_running_loop().create_future()

    async def ask():
        return await box
    task = asyncio.create_task(join(("127.0.0.1", c.port), tmp / name, name=name, ask_code=ask))
    w = await wait_for(lambda: next((x for x in c.waiting.list() if x.name == name), None))
    return task, box, w


def text_of(out):
    return "\n".join(i["text_fallback"] for i in out["intents"])


async def answer_with_dialog(c, intent_id, result, channel=CLAUDE):
    """The owner's answer in the system dialog (played by a fake), through the same route a chat's request for approval takes."""
    c.harness.native_confirm = Dialog(result)
    b = c.harness.begin_confirmation(channel, intent_id, "os_dialog")
    state = await wait_for(lambda: c.harness.ticket(channel, b["ticket"])["state"] != "waiting" and c.harness.ticket(channel, b["ticket"])["state"])
    return b, state


def test_nobody_waiting_says_what_to_do_on_the_new_computer_and_a_waiting_one_is_listed_by_name(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c", answer=None, discovery=True) as c:
            out = c.harness.pairing(CLAUDE)
            assert out["state"] == "idle" and "mas join" in text_of(out) and "No computer is waiting" in text_of(out)
            task, box, w = await waiting_node(c, tmp_path, "lenovo")
            out = c.harness.pairing(CLAUDE)
            assert "1. lenovo" in text_of(out) and "Tell me which one" in text_of(out) and out["intents"][0]["kind"] == "text"
            task.cancel()
    run(go())


def test_the_code_for_the_picked_computer_is_shown_to_the_owner_and_asking_for_it_is_not_an_owner_event(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c", answer=None, discovery=True) as c:
            task, box, w = await waiting_node(c, tmp_path, "lenovo")
            out = c.harness.pairing(CLAUDE, "Lenovo")  # a chat says it any way the owner did
            code = c.owner.view()["code"]
            assert out["state"] == "waiting" and len(code) == 6 and "on your screen" in text_of(out) and "lenovo" in text_of(out)
            assert code not in json.dumps(out)  # what the chat's model reads never holds the code
            assert len(FakeNotice.shown) == 1 and code in FakeNotice.shown[0].message and "lenovo" in FakeNotice.shown[0].message  # the owner's dialog does
            req = events(c, "pairing_offer_requested_by_chat")
            assert len(req) == 1 and req[0].effective_tag != "OWNER" and not events(c, "owner_requested_pairing")  # a tool call is not the owner
            assert code not in json.dumps([e.payload for e in events(c)])  # the code reaches no log
            with pytest.raises(Refused) as e:
                c.harness.pairing(CLAUDE, "lenovo")
            assert e.value.reason == "pairing_busy"
            task.cancel()
    run(go())


def test_the_dialog_is_shown_once_per_code_again_if_the_owner_closed_it_and_taken_down_when_the_code_is_used(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c", answer=None, discovery=True) as c:
            task, box, w = await waiting_node(c, tmp_path, "lenovo")
            c.harness.pairing(CLAUDE, "lenovo")
            c.harness.pairing(CLAUDE)  # asking again while the dialog is open does not pile up dialogs
            assert len(FakeNotice.shown) == 1
            FakeNotice.shown[0].close()  # the owner dismissed it
            out = c.harness.pairing(CLAUDE)
            assert len(FakeNotice.shown) == 2 and c.owner.view()["code"] not in json.dumps(out)
            box.set_result(c.owner.view()["code"])
            await wait_for(lambda: c.owner.view()["status"] == "confirm")
            c.harness.pairing(CLAUDE)
            assert not FakeNotice.shown[1].alive()  # the code is used up: its dialog is gone
            task.cancel()
    run(go())


def test_when_this_computer_cannot_show_a_dialog_the_code_is_still_not_put_in_the_chat(tmp_path, monkeypatch):
    async def go():
        async with coordinator(tmp_path / "c", answer=None, discovery=True) as c:
            c.harness.native_notice = lambda *a, **k: None
            task, box, w = await waiting_node(c, tmp_path, "lenovo")
            out = c.harness.pairing(CLAUDE, "lenovo")
            code = c.owner.view()["code"]
            assert code not in json.dumps(out) and "mas pair" in text_of(out) and "never put in this chat" in text_of(out)
            task.cancel()
    run(go())


def test_a_computer_that_is_not_waiting_or_is_named_twice_is_refused_with_a_reason_the_next_call_can_use(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c", answer=None, discovery=True) as c:
            with pytest.raises(Refused) as e:
                c.harness.pairing(CLAUDE, "ghost")
            assert e.value.reason == "not_waiting" and "mas join" in e.value.message
            t1, _, _ = await waiting_node(c, tmp_path, "twin")
            t2, _, _ = await waiting_node(c, tmp_path / "b", "twin")
            await wait_for(lambda: len([w for w in c.waiting.list() if w.name == "twin"]) == 2)  # both are really on the list before the name is used
            with pytest.raises(Refused) as e2:
                c.harness.pairing(CLAUDE, "twin")
            assert e2.value.reason == "ambiguous_machine"
            t1.cancel()
            t2.cancel()
    run(go())


def test_the_whole_pairing_from_a_chat_needs_the_owners_gesture_and_a_tool_call_cannot_give_it(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c", answer=None, discovery=True) as c:
            task, box, w = await waiting_node(c, tmp_path, "lenovo")
            c.harness.pairing(CLAUDE, "lenovo")
            box.set_result(c.owner.view()["code"])  # the owner types the code on the Lenovo
            await wait_for(lambda: c.owner.view()["status"] == "confirm")
            out = c.harness.pairing(CLAUDE)
            conf = out["intents"][0]
            assert out["state"] == "confirm" and conf["kind"] == "confirm" and "lenovo" in conf["text_fallback"] and "fingerprint" in conf["text_fallback"] and "mas_request_approval" in conf["text_fallback"]
            # what the chat's model can do about it: nothing that completes it
            with pytest.raises(Refused) as e:
                c.harness.decide(CLAUDE, conf["id"], "approve")  # mas_answer
            assert e.value.reason == "confirmation_required"
            b = c.harness.begin_confirmation(CLAUDE, conf["id"], "os_dialog")
            with pytest.raises(Refused) as e2:
                c.harness.complete_confirmation(CLAUDE, b["ticket"], b["plan_hash"], "accept")  # a model claiming the dialog was accepted
            assert e2.value.reason == "not_host_answered"
            with pytest.raises(Refused) as e3:
                c.harness.begin_confirmation(CLAUDE, conf["id"], "panel_click")  # a mechanism the channel does not have
            assert e3.value.reason == "mechanism_unavailable"
            assert c.registry.all() == [] and c.owner.view()["status"] == "confirm"  # still nothing stored
            c.harness.native_confirm = Dialog("approved")  # now the owner answers in the system dialog
            await wait_for(lambda: c.harness.ticket(CLAUDE, b["ticket"])["state"] == "approved")
            pin = await asyncio.wait_for(task, 10)
            assert pin is not None and [n.name for n in c.registry.all()] == ["lenovo"]
            oc = events(c, "owner_confirmation")
            assert oc and oc[-1].effective_tag == "OWNER" and oc[-1].payload["mechanism"] == "os_dialog" and oc[-1].payload["approved"] is True
    run(go())


def test_a_dialog_the_owner_declines_stores_nothing_and_the_node_is_not_paired(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c", answer=None, discovery=True) as c:
            task, box, w = await waiting_node(c, tmp_path, "lenovo")
            c.harness.pairing(CLAUDE, "lenovo")
            box.set_result(c.owner.view()["code"])
            await wait_for(lambda: c.owner.view()["status"] == "confirm")
            conf = c.harness.pairing(CLAUDE)["intents"][0]
            c.harness.native_confirm = Dialog("declined")
            b = c.harness.begin_confirmation(CLAUDE, conf["id"], "os_dialog")
            await wait_for(lambda: c.harness.ticket(CLAUDE, b["ticket"])["state"] == "declined")
            with pytest.raises(Exception):
                await asyncio.wait_for(task, 10)
            assert c.registry.all() == []
            assert "was refused: nothing was stored" in text_of(c.harness.pairing(CLAUDE))  # the last outcome, in words
    run(go())


def test_a_ticket_that_is_not_for_this_plan_cannot_pair_and_neither_can_the_dialog_after_the_plan_changed(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c", answer=None, discovery=True) as c:
            task, box, w = await waiting_node(c, tmp_path, "lenovo")
            c.harness.pairing(CLAUDE, "lenovo")
            box.set_result(c.owner.view()["code"])
            await wait_for(lambda: c.owner.view()["status"] == "confirm")
            conf = c.harness.pairing(CLAUDE)["intents"][0]
            c.harness.native_confirm = Dialog("approved", block=True)
            b = c.harness.begin_confirmation(CLAUDE, conf["id"], "os_dialog")
            c.harness.change_plan(conf["id"], "sha256:" + "0" * 64)  # the question changed while the dialog was open
            c.harness.native_confirm.release.set()
            await wait_for(lambda: c.harness.ticket(CLAUDE, b["ticket"])["state"] == "rejected" or c.harness.ticket(CLAUDE, b["ticket"])["state"] != "waiting")
            assert c.registry.all() == [] and c.owner.view()["status"] != "accepted"
            task.cancel()
    run(go())


def test_unpair_removes_nothing_until_the_owner_answers_and_a_tool_call_cannot_answer(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c", answer=True) as c:
            c.desk.open_offer("o1", "482915")
            await pair_node(c, tmp_path, "482915", node="lenovo")
            await wait_for(lambda: c.registry.all())
            node = c.registry.all()[0]
            out = c.harness.unpair(CLAUDE, "LENOVO")
            conf = out["intents"][0]
            assert conf["kind"] == "confirm" and "lenovo" in conf["text_fallback"] and "mas join" in conf["text_fallback"] and "removed from this coordinator at once" in conf["text_fallback"]
            assert [n.node_id for n in c.registry.all()] == [node.node_id]  # asking removed nothing
            with pytest.raises(Refused) as e:
                c.harness.decide(CLAUDE, conf["id"], "approve")
            assert e.value.reason == "confirmation_required"
            b, state = await answer_with_dialog(c, conf["id"], "approved")
            assert state == "approved"
            await wait_for(lambda: not c.registry.all())
            un = events(c, "node_unpaired")
            assert len(un) == 1 and un[0].effective_tag == "OWNER" and un[0].payload["via"] == "claude-desktop:os_dialog"
    run(go())


def test_an_unpair_the_owner_declines_leaves_the_computer_paired(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c", answer=True) as c:
            c.desk.open_offer("o1", "482915")
            await pair_node(c, tmp_path, "482915", node="lenovo")
            await wait_for(lambda: c.registry.all())
            conf = c.harness.unpair(CLAUDE, "lenovo")["intents"][0]
            _, state = await answer_with_dialog(c, conf["id"], "declined")
            assert state == "declined" and len(c.registry.all()) == 1 and not events(c, "node_unpaired")
    run(go())


def test_unpairing_a_computer_that_is_not_paired_is_refused_and_asks_nothing(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c", answer=True) as c:
            with pytest.raises(Refused) as e:
                c.harness.unpair(CLAUDE, "nobody")
            assert e.value.reason == "unknown_node" and not c.harness.pending_confirmations()
    run(go())


def test_the_panel_can_answer_the_same_confirmations_because_it_is_the_owners_own_screen(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c", answer=True) as c:
            c.desk.open_offer("o1", "482915")
            await pair_node(c, tmp_path, "482915", node="lenovo")
            await wait_for(lambda: c.registry.all())
            conf = c.harness.unpair(CLAUDE, "lenovo")["intents"][0]
            pend = c.harness.pending_confirmations()
            assert [p["intent"] for p in pend] == [conf["id"]]
            c.harness.panel_decide(conf["id"], pend[0]["plan_hash"], True)  # the owner's click
            await wait_for(lambda: not c.registry.all())
    run(go())


def test_the_chat_reaches_both_tools_over_the_real_http_route_and_the_server_tells_the_chat_how_to_use_them(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c", answer=None, discovery=True, web=True) as c:
            client = CoreClient(c.harness.dir, "claude-desktop")
            out = await asyncio.to_thread(client.pairing)
            assert out["state"] == "idle" and "mas join" in out["intents"][0]["text_fallback"]
            with pytest.raises(Exception) as e:
                await asyncio.to_thread(client.unpair, "ghost")
            assert "unknown_node" in str(e.value)
    run(go())
    assert {"mas_pair", "mas_unpair"} <= set(mcp_server.TOOLS)
    assert "mas_request_approval" in mcp_server.TOOLS["mas_pair"][0] and "cannot complete a pairing" in mcp_server.TOOLS["mas_pair"][0]
    ins = mcp_server.instructions()
    assert "pair a new machine" in ins and "never recommend" in ins.lower() or "Never recommend" in ins  # the knowledge is the skill's, delivered with the connection


def test_every_owner_facing_pairing_word_is_english_ascii_and_in_the_strings_module():
    from envrec_harness import strings as S
    for k, v in S.STR["en"].items():
        if k.startswith(("pr_", "un_")):
            assert v.isascii(), (k, v)


def test_the_whole_flow_through_the_chat_adapter_the_code_the_wait_the_confirmation_and_the_owners_dialog(tmp_path):
    """The tools as the chat calls them (`tools/call` on the MCP adapter, over the real HTTP route of the core): ask for the code, wait for the owner to type it, get the confirmation, and approve it with
    the system dialog (a fake the test controls) through `mas_request_approval`. A host that cannot show a form uses the system dialog, which is the path the owner's Mac takes."""
    import io

    async def go():
        async with coordinator(tmp_path / "c", answer=None, discovery=True, web=True) as c:
            adapter = mcp_server.McpAdapter(CoreClient(c.harness.dir, "claude-desktop"), out=io.StringIO())
            call = lambda name, **args: asyncio.to_thread(adapter.tools_call, {"name": name, "arguments": args})  # noqa: E731
            task, box, w = await waiting_node(c, tmp_path, "lenovo")
            listing = await call("mas_pair")
            assert "1. lenovo" in listing["content"][0]["text"]
            results = [listing]
            shown = await call("mas_pair", machine="lenovo")
            code = c.owner.view()["code"]
            results.append(shown)
            assert "on your screen" in shown["content"][0]["text"] and shown["structuredContent"]["state"] == "waiting"
            assert len(FakeNotice.shown) == 1 and code in FakeNotice.shown[0].message  # the owner reads it in the dialog
            for name in ("mas_network", "mas_activity", "mas_pair"):  # every other tool a chat could ask while the code is live
                results.append(await call(name))
            box.set_result(code)  # the owner types it on the Lenovo, while the chat waits
            confirm = await call("mas_pair", wait_s=10)
            results.append(confirm)
            assert confirm["structuredContent"]["state"] == "confirm"
            intent_id = confirm["structuredContent"]["intents"][0]["id"]
            c.harness.native_confirm = Dialog("approved")
            done = await call("mas_request_approval", intent_id=intent_id)
            assert done["structuredContent"]["state"] == "approved" and done["structuredContent"]["mechanism"] == "os_dialog"
            await asyncio.wait_for(task, 10)
            assert [n.name for n in c.registry.all()] == ["lenovo"]
            un = await call("mas_unpair", machine="lenovo")
            assert un["structuredContent"]["intents"][0]["kind"] == "confirm" and len(c.registry.all()) == 1  # asking removed nothing
            c.harness.native_confirm = Dialog("approved")
            results.append(await call("mas_request_approval", intent_id=un["structuredContent"]["intents"][0]["id"]))
            await wait_for(lambda: not c.registry.all())
            results += [done, un]
            assert all(code not in json.dumps(r) for r in results), "the pairing code reached a tool result"  # text, structured content and the cards for the host: none of them
    run(go())


def _minimal_args(schema: dict) -> dict:
    """Arguments that satisfy a tool's own input schema, from the schema alone (a test of what a result carries, not of what the tool does with the arguments)."""
    out = {}
    for name in schema.get("required", []):
        spec = (schema.get("properties") or {}).get(name, {})
        out[name] = (spec.get("enum") or [None])[0] if spec.get("enum") else {"string": "x", "integer": 1, "number": 1, "boolean": False, "object": {}, "array": []}.get(spec.get("type"), "x")
    return out


def test_no_tool_the_core_publishes_puts_the_live_pairing_code_in_a_result_whatever_it_is_asked(tmp_path):
    """Every tool of the chat door, the entry tools AND the services the core lists as data, is called with the pairing code live and with arguments that satisfy its schema. Refusals count: a typed error
    is a result too. The code reaches the owner's dialog only. SIMULATED-LOCAL."""
    import io

    async def go():
        async with coordinator(tmp_path / "c", answer=None, discovery=True, web=True) as c:
            adapter = mcp_server.McpAdapter(CoreClient(c.harness.dir, "claude-desktop"), out=io.StringIO())
            task, box, w = await waiting_node(c, tmp_path, "lenovo")
            await asyncio.to_thread(adapter.tools_call, {"name": "mas_pair", "arguments": {"machine": "lenovo"}})
            code = c.owner.view()["code"]
            assert len(code) == 6 and len(FakeNotice.shown) == 1 and code in FakeNotice.shown[0].message
            listed = adapter.tools_list({})["tools"]
            core_names = set(adapter._core_tools())
            assert len(core_names) >= 9 and core_names <= {t["name"] for t in listed}  # the services are listed from the core's data
            seen = []
            for t in listed:
                if t["name"] in ("mas_pair", "mas_unpair", "mas_request_approval"):
                    continue  # pair is the tool that shows the dialog (covered above and in the whole-flow test); the others only ask for a confirmation the owner answers
                args = _minimal_args(t["inputSchema"])
                try:
                    r = await asyncio.wait_for(asyncio.to_thread(adapter.tools_call, {"name": t["name"], "arguments": args}), 20)
                except KeyError:
                    continue  # an app-only tool the model never calls
                seen.append(t["name"])
                assert code not in json.dumps(r, default=str), f"{t['name']} put the pairing code in its result"
            assert core_names <= set(seen), core_names - set(seen)  # every service was actually called
            assert code not in json.dumps([e.payload for e in events(c)], default=str)  # and none of them logged it
            task.cancel()
    run(go())

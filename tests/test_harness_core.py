"""The Harness App core on the coordinator: channels, intents, the owner's decisions, and above all who may approve a WRITE.
SIMULATED-LOCAL: a real coordinator and a real paired node on loopback, a scripted model; the operating system's dialog is a fake that a test controls (the real one is shown only in a supervised run)."""
from __future__ import annotations

import asyncio
import http.client
import json
import threading

import pytest

from envrec.model import ScriptedModel
from envrec_coordinator import harness as H
from envrec_coordinator.harness import CHANNELS, Refused
from mas_world import Clock, coordinator
from test_mas_agent import ANALYSIS_SCRIPT, AVAILABLE, answer_when_asked, down, node_up, options_script

HASH = "sha256:" + "cd" * 32
OTHER = "sha256:" + "ef" * 32
CLAUDE, OWUI, PANEL = CHANNELS["claude-desktop"], CHANNELS["openwebui"], CHANNELS["panel"]


def run(coro):
    return asyncio.run(coro)


class Dialog:
    """The operating system's confirmation, played by a test: it can answer at once, or hold until released (to change the plan while it is open)."""

    def __init__(self, result="approved", block=False):
        self.result, self.block, self.calls, self.release, self.opened = result, block, [], threading.Event(), threading.Event()

    def __call__(self, title, message, **kw):
        self.calls.append((title, message))
        self.opened.set()
        if self.block:
            self.release.wait(10)
        return {"result": self.result, "mechanism": "fake_dialog", "strong": None}


@pytest.fixture(autouse=True)
def os_dialog_exists(monkeypatch):
    monkeypatch.setattr(H.osconfirm, "available", lambda *a, **k: {"available": True, "mechanism": "fake_dialog"})


def ask_write(c, **kw):
    return c.harness.request_confirmation(None, summary="Install llama.cpp", plan_hash=kw.pop("plan_hash", HASH), command="curl -L X | tar x", undo="rm -r ~/mas/llama", **kw)


def owner_events(c, type_):
    return [e for e in c.log.events(type_)]


async def settle(pred, timeout=5.0):
    for _ in range(int(timeout / 0.02)):
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("never happened")


# ---- who is asking ---------------------------------------------------------------------------------------------------------------------------

def test_a_channel_is_the_one_its_token_belongs_to_and_the_tokens_are_private_and_never_logged(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            h = c.harness
            assert h.authenticate(h.tokens["claude-desktop"]) is CLAUDE and h.authenticate(h.tokens["openwebui"]) is OWUI
            assert h.authenticate("") is None and h.authenticate("x" * 43) is None and h.authenticate(h.tokens["claude-desktop"] + "x") is None
            assert h.tokens["claude-desktop"] != h.tokens["openwebui"]
            for name in ("claude-desktop", "openwebui"):
                assert c.layer.is_private(h.dir / f"{name}.token")
            dumped = json.dumps([e for e in c.log.events()], default=str)
            assert not any(tok in dumped for tok in h.tokens.values())
    run(go())


# ---- a model's tool call can never approve a write ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("ch", [CLAUDE, OWUI, PANEL])
def test_an_owner_decision_call_never_approves_a_write_whoever_makes_it(tmp_path, ch):
    async def go():
        async with coordinator(tmp_path) as c:
            p = ask_write(c)
            for option in ("approve", "accept", "yes", "confirm", HASH):
                with pytest.raises(Refused) as e:
                    c.harness.decide(ch, p.intent.id, option)
                assert e.value.reason == "confirmation_required"
            await asyncio.sleep(0.05)
            assert not p.future.done() and p.intent.extra["state"] == "pending"
            assert owner_events(c, "owner_confirmation") == [] and len(owner_events(c, "decision_refused")) == 5
            assert not any(e.tag == "OWNER" for e in c.log.events() if e.type.startswith(("owner_confirmation", "harness_decision")))
    run(go())


def test_a_confirmation_that_a_cloud_model_asks_for_cannot_use_the_panels_or_another_channels_mechanism(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            p = ask_write(c)
            for m in ("panel_click", "host_dialog", "made_up"):
                with pytest.raises(Refused) as e:
                    c.harness.begin_confirmation(CLAUDE, p.intent.id, m)
                assert e.value.reason == "mechanism_unavailable"
            assert not p.future.done()
    run(go())


# ---- host elicitation: answered by the host's UI, bound to the plan hash ----------------------------------------------------------------------

def test_elicitation_is_offered_only_after_the_live_handshake_said_the_host_supports_it(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            p = ask_write(c)
            assert c.harness.mechanisms(CLAUDE) == ["os_dialog"]
            with pytest.raises(Refused):
                c.harness.begin_confirmation(CLAUDE, p.intent.id, "host_elicitation")
            c.harness.declare(CLAUDE, {"elicitation": True, "mcp_apps": True, "protocol": "2025-11-25"})
            assert c.harness.mechanisms(CLAUDE) == ["host_elicitation", "os_dialog"]
            assert owner_events(c, "harness_channel_capabilities")[-1].payload["capabilities"]["elicitation"] is True
    run(go())


def test_an_accepted_elicitation_for_the_exact_plan_hash_is_the_owner_and_resolves_the_protocol(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            c.harness.declare(CLAUDE, {"elicitation": True})
            p = ask_write(c)
            b = c.harness.begin_confirmation(CLAUDE, p.intent.id, "host_elicitation")
            assert b["plan_hash"] == HASH and HASH in b["prompt"]["message"] and "curl -L X | tar x" in b["prompt"]["message"] and "rm -r ~/mas/llama" in b["prompt"]["message"]
            assert c.harness.complete_confirmation(CLAUDE, b["ticket"], HASH, "accept")["state"] == "approved"
            assert await asyncio.wait_for(p.future, 1) is True
            ev = owner_events(c, "owner_confirmation")
            assert len(ev) == 1 and ev[0].tag == "OWNER" and ev[0].payload["channel"] == "claude-desktop" and ev[0].payload["mechanism"] == "host_elicitation" and ev[0].payload["plan_hash"] == HASH
            assert p.intent.extra["state"] == "approved"
    run(go())


def test_a_declined_elicitation_resolves_false_and_is_recorded_as_the_owners_no(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            c.harness.declare(CLAUDE, {"elicitation": True})
            p = ask_write(c)
            b = c.harness.begin_confirmation(CLAUDE, p.intent.id, "host_elicitation")
            c.harness.complete_confirmation(CLAUDE, b["ticket"], HASH, "decline")
            assert await asyncio.wait_for(p.future, 1) is False and owner_events(c, "owner_confirmation")[0].payload["approved"] is False
    run(go())


def test_an_elicitation_bound_to_a_different_plan_hash_is_rejected_and_burns_its_ticket(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            c.harness.declare(CLAUDE, {"elicitation": True})
            p = ask_write(c)
            b = c.harness.begin_confirmation(CLAUDE, p.intent.id, "host_elicitation")
            with pytest.raises(Refused) as e:
                c.harness.complete_confirmation(CLAUDE, b["ticket"], OTHER, "accept")
            assert e.value.reason == "plan_hash_mismatch" and not p.future.done()
            rej = owner_events(c, "confirmation_rejected")[-1].payload
            assert rej["ticket_hash"] == HASH and rej["presented_hash"] == OTHER
            with pytest.raises(Refused) as e2:  # the ticket is spent: the right hash now cannot revive it
                c.harness.complete_confirmation(CLAUDE, b["ticket"], HASH, "accept")
            assert e2.value.reason == "ticket_used" and not p.future.done() and owner_events(c, "owner_confirmation") == []
    run(go())


def test_a_plan_that_changed_after_the_ticket_was_made_cannot_be_approved_with_the_old_hash(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            c.harness.declare(CLAUDE, {"elicitation": True})
            p = ask_write(c)
            b = c.harness.begin_confirmation(CLAUDE, p.intent.id, "host_elicitation")
            c.harness.change_plan(p.intent.id, OTHER)
            with pytest.raises(Refused) as e:
                c.harness.complete_confirmation(CLAUDE, b["ticket"], HASH, "accept")
            assert e.value.reason == "plan_hash_mismatch" and not p.future.done()
    run(go())


def test_a_ticket_is_the_channels_own_single_use_and_short_lived(tmp_path):
    async def go():
        clock = Clock()
        async with coordinator(tmp_path, clock=clock) as c:
            c.harness.declare(CLAUDE, {"elicitation": True})
            c.harness.declare(OWUI, {"event_call": True})
            p = ask_write(c)
            b = c.harness.begin_confirmation(CLAUDE, p.intent.id, "host_elicitation")
            with pytest.raises(Refused) as e:  # another channel cannot answer it
                c.harness.complete_confirmation(OWUI, b["ticket"], HASH, "accept")
            assert e.value.reason == "unknown_ticket"
            clock.advance(H.TICKET_TTL_S + 1)
            with pytest.raises(Refused) as e2:
                c.harness.complete_confirmation(CLAUDE, b["ticket"], HASH, "accept")
            assert e2.value.reason == "ticket_expired" and not p.future.done()
            b2 = c.harness.begin_confirmation(CLAUDE, p.intent.id, "host_elicitation")
            c.harness.complete_confirmation(CLAUDE, b2["ticket"], HASH, "accept")
            with pytest.raises(Refused) as e3:
                c.harness.complete_confirmation(CLAUDE, b2["ticket"], HASH, "accept")
            assert e3.value.reason in ("ticket_used", "nothing_to_confirm")
            assert len(owner_events(c, "owner_confirmation")) == 1
    run(go())


# ---- the native confirmation of the operating system -----------------------------------------------------------------------------------------

def test_without_elicitation_the_coordinator_raises_the_operating_systems_own_dialog_with_the_plan_and_its_hash(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            dlg = Dialog("approved")
            c.harness.native_confirm = dlg
            p = ask_write(c)
            b = c.harness.begin_confirmation(CLAUDE, p.intent.id, "os_dialog")
            assert await asyncio.wait_for(p.future, 5) is True
            title, message = dlg.calls[0]
            assert HASH in message and "curl -L X | tar x" in message and "rm -r ~/mas/llama" in message and "Install llama.cpp" in message
            assert c.harness.ticket(CLAUDE, b["ticket"])["state"] == "approved"
            ev = owner_events(c, "owner_confirmation")[0]
            assert ev.tag == "OWNER" and ev.payload["mechanism"] == "os_dialog" and ev.payload["channel"] == "claude-desktop" and ev.payload["plan_hash"] == HASH and ev.payload["os_mechanism"] == "fake_dialog"
    run(go())


def test_the_callers_cannot_answer_for_the_operating_systems_dialog(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            dlg = Dialog(block=True)
            c.harness.native_confirm = dlg
            p = ask_write(c)
            b = c.harness.begin_confirmation(CLAUDE, p.intent.id, "os_dialog")
            await asyncio.to_thread(dlg.opened.wait, 5)
            with pytest.raises(Refused) as e:
                c.harness.complete_confirmation(CLAUDE, b["ticket"], HASH, "accept")
            assert e.value.reason == "not_host_answered" and not p.future.done()
            dlg.release.set()
            await asyncio.wait_for(p.future, 5)
    run(go())


def test_an_os_confirmation_whose_plan_changed_while_the_dialog_was_open_is_rejected(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            dlg = Dialog("approved", block=True)
            c.harness.native_confirm = dlg
            p = ask_write(c)
            b = c.harness.begin_confirmation(CLAUDE, p.intent.id, "os_dialog")
            await asyncio.to_thread(dlg.opened.wait, 5)
            c.harness.change_plan(p.intent.id, OTHER)  # the owner is looking at the old plan's hash
            dlg.release.set()
            await settle(lambda: c.harness.ticket(CLAUDE, b["ticket"])["state"] != "waiting")
            assert c.harness.ticket(CLAUDE, b["ticket"]) == {"ticket": b["ticket"], "state": "rejected", "reason": "plan_hash_mismatch"} or c.harness.ticket(CLAUDE, b["ticket"])["state"] == "rejected"
            assert not p.future.done() and owner_events(c, "owner_confirmation") == []
    run(go())


def test_one_native_dialog_at_a_time_and_a_dialog_nobody_answers_approves_nothing(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            dlg = Dialog("timeout", block=True)
            c.harness.native_confirm = dlg
            p = ask_write(c)
            b = c.harness.begin_confirmation(CLAUDE, p.intent.id, "os_dialog")
            await asyncio.to_thread(dlg.opened.wait, 5)
            again = c.harness.begin_confirmation(CLAUDE, p.intent.id, "os_dialog")  # the same question asked again joins the dialog that is open: it does not open a second one
            assert again["ticket"] == b["ticket"] and len(dlg.calls) == 1
            other = ask_write(c, plan_hash=OTHER)  # a DIFFERENT question cannot open a second native window while one is open
            with pytest.raises(Refused) as e:
                c.harness.begin_confirmation(CLAUDE, other.intent.id, "os_dialog")
            assert e.value.reason == "os_dialog_busy"
            dlg.release.set()
            await settle(lambda: c.harness.ticket(CLAUDE, b["ticket"])["state"] != "waiting")
            assert c.harness.ticket(CLAUDE, b["ticket"])["state"] == "timeout" and not p.future.done() and p.intent.extra["state"] == "pending"
    run(go())


def test_with_no_native_dialog_available_the_panel_is_the_way_and_its_click_is_the_owner(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            c.harness.native_confirm = Dialog("unavailable")
            p = ask_write(c)
            b = c.harness.begin_confirmation(CLAUDE, p.intent.id, "os_dialog")
            await settle(lambda: c.harness.ticket(CLAUDE, b["ticket"])["state"] != "waiting")
            assert c.harness.ticket(CLAUDE, b["ticket"])["state"] == "unavailable" and not p.future.done()
            assert c.snapshot()["confirmations"][0]["plan_hash"] == HASH  # the panel shows it
            with pytest.raises(Refused) as e:
                c.harness.panel_decide(p.intent.id, OTHER, True)
            assert e.value.reason == "plan_hash_mismatch" and not p.future.done()
            c.harness.panel_decide(p.intent.id, HASH, True)
            assert await asyncio.wait_for(p.future, 1) is True
            assert owner_events(c, "owner_confirmation")[-1].payload["mechanism"] == "panel_click"
    run(go())


def test_open_webui_confirms_with_its_own_dialog_once_it_said_it_can_and_the_hash_still_binds(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            p = ask_write(c)
            with pytest.raises(Refused):
                c.harness.begin_confirmation(OWUI, p.intent.id, "host_dialog")
            c.harness.declare(OWUI, {"event_call": True})
            b = c.harness.begin_confirmation(OWUI, p.intent.id, "host_dialog")
            with pytest.raises(Refused):
                c.harness.complete_confirmation(OWUI, b["ticket"], OTHER, "accept")
            b = c.harness.begin_confirmation(OWUI, p.intent.id, "host_dialog")
            c.harness.complete_confirmation(OWUI, b["ticket"], HASH, "accept")
            assert await asyncio.wait_for(p.future, 1) is True and owner_events(c, "owner_confirmation")[0].payload["channel"] == "openwebui"
    run(go())


# ---- sensitivity ---------------------------------------------------------------------------------------------------------------------------------

def test_a_cloud_channel_cannot_create_a_confidential_run_and_never_sees_a_confidential_intent(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            with pytest.raises(Refused) as e:
                c.harness.start(CLAUDE, "analyze", "nid", "confidential")
            assert e.value.status == 403 and e.value.reason == "cloud_cannot_create_confidential" and owner_events(c, "harness_refused")
            p = ask_write(c, sensitivity="confidential")
            seen = c.harness.for_channel(p.intent, CLAUDE)
            assert seen["kind"] == "text" and seen["payload"] == {"withheld": True} and "plan" not in seen["text_fallback"] and HASH not in json.dumps(seen) and "curl" not in json.dumps(seen)
            assert c.harness.for_channel(p.intent, OWUI)["payload"]["command"] == "curl -L X | tar x"  # a local chat may see it
            c.harness.declare(CLAUDE, {"elicitation": True})
            with pytest.raises(Refused) as e2:
                c.harness.begin_confirmation(CLAUDE, p.intent.id, "host_elicitation")
            assert e2.value.reason == "withheld_from_cloud"
    run(go())


def test_what_a_cloud_channel_receives_is_filtered_capped_and_logged(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            i = c.harness._add("r", "show_data", "GUARD", "internal", {"data_kind": "network", "data": {"nodes": [{"name": "A", "fingerprint": "SHA256:abc", "ip": "192.168.1.5", "paths": ["/Users/x"], "ram": 8}]}}, "text", "r:d")
            d = c.harness.for_channel(i, CLAUDE)
            node = d["payload"]["data"]["nodes"][0]
            assert node == {"name": "A", "ram": 8}
            assert "fingerprint" in json.dumps(c.harness.for_channel(i, OWUI)["payload"])
            eg = owner_events(c, "cloud_egress")
            assert len(eg) == 1 and eg[0].payload["intent"] == "r:d" and eg[0].payload["channel"] == "claude-desktop"
            c.harness.for_channel(i, CLAUDE)
            assert len(owner_events(c, "cloud_egress")) == 1
            big = c.harness._add("r", "show_data", "GUARD", "internal", {"data_kind": "machine", "data": {"x": "y" * (H.MAX_CLOUD_BYTES + 5)}}, "t" * 3000, "r:big")
            assert c.harness.for_channel(big, CLAUDE)["payload"].get("truncated") is True
    run(go())


# ---- a real run through the core -----------------------------------------------------------------------------------------------------------------

def test_a_chat_answers_a_closed_question_and_picks_an_option_tagged_with_its_channel(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            agent, task, nid = await node_up(c, tmp_path)
            try:
                a = c.runs.start_analysis(nid, "chat:claude-desktop", model=ScriptedModel(list(ANALYSIS_SCRIPT)), status=AVAILABLE)
                await asyncio.wait_for(a.task, 60)
                out = c.harness.intents(CLAUDE, a.id)
                kinds = [i["payload"].get("data_kind") for i in out["intents"] if i["kind"] == "show_data"]
                assert "machine" in kinds and "run" in kinds and out["run"]["finished"]
                tags = {i["actor"] for i in out["intents"] if i["kind"] == "progress"}
                assert {"MODEL", "GUARD", "OWNER"} <= tags  # every step says who acted; the chat's request is the owner's, with its channel in the log
                req = [e for e in a.store.events() if e.type == "run_requested"][0]
                assert req.payload["via"] == "chat:claude-desktop" and req.effective_tag == "OWNER"
                holder = {}
                o = c.runs.start_options(nid, "chat:claude-desktop", model=ScriptedModel(options_script(holder)), status=AVAILABLE)
                holder["run"] = o
                await settle(lambda: o.adapter is not None and o.adapter.pending is not None)
                q = [i for i in c.harness.intents(CLAUDE, o.id)["intents"] if i["kind"] == "ask_choice"][0]
                assert "1. " in q["text_fallback"] and q["payload"]["writes"] is False
                assert c.harness.decide(CLAUDE, q["id"], "local") == {"ok": True, "channel": "claude-desktop", "relayed_by_model": True}
                await asyncio.wait_for(o.task, 60)
                ans = [e for e in o.store.events() if e.type == "owner_answer"][0]
                assert ans.payload["channel"] == "claude-desktop" and ans.effective_tag == "OWNER"
                fin = c.harness.intents(CLAUDE, o.id)
                pick = [i for i in fin["intents"] if i["id"].endswith(":pick")][0]
                assert pick["state"] == "pending" and "1. " in pick["text_fallback"]
                c.harness.decide(CLAUDE, pick["id"], pick["payload"]["options"][0]["id"])
                picked = [e for e in o.store.events() if e.type == "owner_pick_option"][0]
                assert picked.payload["channel"] == "claude-desktop"
                with pytest.raises(Refused):  # the same question cannot be answered twice
                    c.harness.decide(CLAUDE, pick["id"], pick["payload"]["options"][0]["id"])
                card = c.harness.card(CLAUDE, f"{o.id}:data:options", "mcp", True)["html"]
                assert "data-choose" in card and "provisional" in card
                assert "data-choose" not in c.harness.card(OWUI, f"{o.id}:data:options", "openwebui", True)["html"]  # an Open WebUI embed cannot call back
            finally:
                await down(agent, task)
    run(go())


def test_the_network_map_comes_from_the_coordinator_and_the_owner_sets_the_kind_of_each_machine(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            agent, task, nid = await node_up(c, tmp_path)
            try:
                d = c.harness.network_intent(CLAUDE)
                assert d["payload"]["data_kind"] == "network" and d["payload"]["data"]["nodes"][0]["status"] in ("online", "busy") and d["payload"]["data"]["nodes"][0]["kind"] == "computer"  # busy while its own report task runs
                assert "node1" in d["text_fallback"]
                c.harness.set_kind(nid, "laptop")
                assert c.harness.network_data()["nodes"][0]["kind"] == "laptop"
                with pytest.raises(Refused):
                    c.harness.set_kind(nid, "toaster")
                with pytest.raises(Refused):
                    c.harness.set_kind("nobody", "laptop")
            finally:
                await down(agent, task)
    run(go())


# ---- the door: /harness/v1 on the owner's loopback screen --------------------------------------------------------------------------------------------

def call(c, method, path, *, token=None, body=None, headers=None, host=None):
    h = {"Host": host or f"127.0.0.1:{c.web.port}", **(headers or {})}
    if token:
        h["Authorization"] = f"Bearer {token}"
    if body is not None:
        h["Content-Type"] = "application/json"
    conn = http.client.HTTPConnection("127.0.0.1", c.web.port, timeout=10)
    try:
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
        r = conn.getresponse()
        data = r.read()
        return r.status, (json.loads(data) if data.startswith(b"{") else data)
    finally:
        conn.close()


def test_the_harness_door_wants_a_channel_token_and_refuses_browsers_cookies_and_other_hosts(tmp_path):
    async def go():
        async with coordinator(tmp_path, web=True) as c:
            tok = c.harness.tokens["claude-desktop"]
            a = lambda *x, **k: asyncio.to_thread(call, c, *x, **k)  # noqa: E731
            assert (await a("GET", "/harness/v1/capabilities"))[0] == 401
            assert (await a("GET", "/harness/v1/capabilities", token="wrong"))[0] == 401
            assert (await a("GET", "/harness/v1/capabilities", token=tok, headers={"Origin": "http://evil.example"}))[0] == 403  # a web page
            assert (await a("GET", "/harness/v1/capabilities", token=tok, headers={"Cookie": "x=y"}))[0] == 403
            assert (await a("GET", "/harness/v1/capabilities", token=tok, host="evil.example:80"))[0] == 403  # DNS rebinding
            s, cap = await a("GET", "/harness/v1/capabilities", token=tok)
            assert s == 200 and cap["channel"] == "claude-desktop" and cap["cloud"] is True and cap["model_in_middle"] is True and cap["confirmations"] == ["os_dialog"]
            assert (await a("GET", "/api/state", token=tok))[0] == 401  # a channel token is not the owner's session
            assert json.loads((c.harness.dir / "endpoint.json").read_text()) == {"host": "127.0.0.1", "port": c.web.port}
    run(go())


def test_over_http_a_tool_call_cannot_approve_a_write_and_the_channel_is_not_a_header_the_client_writes(tmp_path):
    async def go():
        async with coordinator(tmp_path, web=True) as c:
            a = lambda *x, **k: asyncio.to_thread(call, c, *x, **k)  # noqa: E731
            tok = c.harness.tokens["claude-desktop"]
            p = ask_write(c)
            s, out = await a("POST", "/harness/v1/decisions", token=tok, body={"intent_id": p.intent.id, "option": "approve"})
            assert s == 409 and out["error"] == "confirmation_required" and not p.future.done()
            s, out = await a("POST", "/harness/v1/capabilities", token=tok, body={"declared": {"elicitation": True}}, headers={"X-MAS-Channel": "panel"})
            assert out["channel"] == "claude-desktop"
            s, b = await a("POST", "/harness/v1/confirmations", token=tok, body={"intent_id": p.intent.id, "mechanism": "host_elicitation"})
            assert s == 200 and b["plan_hash"] == HASH
            s, out = await a("POST", f"/harness/v1/confirmations/{b['ticket']}/complete", token=tok, body={"plan_hash": OTHER, "action": "accept"})
            assert s == 409 and out["error"] == "plan_hash_mismatch" and not p.future.done()
            s, b = await a("POST", "/harness/v1/confirmations", token=tok, body={"intent_id": p.intent.id, "mechanism": "host_elicitation"})
            s, out = await a("POST", f"/harness/v1/confirmations/{b['ticket']}/complete", token=tok, body={"plan_hash": HASH, "action": "accept"})
            assert s == 200 and out["state"] == "approved" and p.future.result() is True
    run(go())


def test_the_owner_panels_click_approves_over_the_session_with_csrf_and_the_hash(tmp_path):
    from test_mas_web import Browser

    async def go():
        async with coordinator(tmp_path, web=True) as c:
            b = Browser(c)
            await b.open()
            await b.state()
            p = ask_write(c)
            assert (await b.post("/api/confirm/decide", {"intent": p.intent.id, "plan_hash": OTHER, "accept": True}))[0] == 409 and not p.future.done()
            assert (await b.post("/api/confirm/decide", {"intent": p.intent.id, "plan_hash": HASH, "accept": True}, csrf=False))[0] == 403
            p2 = ask_write(c, plan_hash=HASH)
            s, _ = await b.post("/api/confirm/decide", {"intent": p2.intent.id, "plan_hash": HASH, "accept": True})
            assert s == 200 and p2.future.result() is True
            assert owner_events(c, "owner_confirmation")[-1].payload["mechanism"] == "panel_click"
    run(go())


def test_asking_again_for_the_same_plan_is_the_same_question(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            a, b = ask_write(c), ask_write(c)
            assert a is b and len([i for log in c.harness._logs.values() for i in log if i.kind == "confirm"]) == 1
    run(go())


def test_the_owner_panel_home_is_the_network_map_in_a_frame_of_its_own_origin_with_nothing_inline(tmp_path):
    from test_mas_web import Browser

    async def go():
        async with coordinator(tmp_path, web=True) as c:
            b = Browser(c)
            assert (await b.raw("GET", "/card/network", cookie=False))[0] == 401 and (await b.raw("GET", "/cards.js", cookie=False))[0] == 401
            await b.open()
            s, hdr, doc = await b.raw("GET", "/card/network")
            text = doc.decode()
            assert s == 200 and 'href="/cards.css"' in text and 'src="/cards.js"' in text and "<style" not in text and "<script>" not in text and ' style="' not in text and "data-card=\"network\"" in text
            assert "frame-ancestors 'self'" in hdr["content-security-policy"] and "'unsafe-inline'" not in hdr["content-security-policy"] and hdr["x-frame-options"] == "SAMEORIGIN"
            s, hdr, _ = await b.raw("GET", "/")
            assert "frame-ancestors 'none'" in hdr["content-security-policy"] and "frame-src 'self'" in hdr["content-security-policy"] and hdr["x-frame-options"] == "DENY"  # the page itself is still never framed
            for path, ctype in (("/cards.css", "text/css"), ("/cards.js", "text/javascript")):
                s, hdr, body = await b.raw("GET", path)
                assert s == 200 and hdr["content-type"].startswith(ctype) and b"http://" not in body and b"https://" not in body
            assert "no-store" in hdr["cache-control"]
    run(go())


def test_a_start_or_answer_by_the_operator_is_logged_as_the_operators_and_never_as_the_owner(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            agent, task, nid = await node_up(c, tmp_path)
            try:
                v = c.harness.start(CLAUDE, "analyze", "node1", "internal", "claude-code")  # a name is enough
                assert v["via"] == "claude-code"
                run_ = c.runs.runs[v["id"]]
                req = [e for e in run_.store.events() if e.type == "run_requested"][0]
                assert req.effective_tag == "CLAUDE-CODE" and not any(e.effective_tag == "OWNER" for e in run_.store.events())
                run_.task.cancel()
            finally:
                await down(agent, task)
    run(go())

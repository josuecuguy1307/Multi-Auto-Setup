"""Preparing a machine from a chat: the setup run through the Harness App core, and above all WHO can approve a step that writes.
SIMULATED-LOCAL: a real coordinator and a real node agent on loopback (T3a's world: the runtime and the model file come from a web server on loopback, the runtime is a small program with the real
server's flags), a scripted model, and a fake for the operating system's dialog that a test controls (the real dialog is shown only in a supervised run). Nothing is installed outside the test's folder.
Skipped on Windows, where the test runtime cannot run (UNTESTED there)."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import re

import pytest

import envrec_os
from envrec.model import ScriptedModel
from envrec_os import procs
from envrec_coordinator import harness as H
from envrec_coordinator.harness import CHANNELS, Refused
from mas_world import coordinator
from test_harness_core import Dialog, call
from envrec_harness.intents import for_display as D, undisplay
from test_harness_mcp import FakeHost
from test_harness_openwebui import Chat, load_pipe
from test_executors import Origin
from test_mas_agent import AVAILABLE, act, analysed, node_up
from test_setup_run import LOOSE, make_world, record_pick, tree

pytestmark = pytest.mark.skipif(envrec_os.family() == "windows", reason="the test runtime is a POSIX program (the Windows branch is UNTESTED)")
CLAUDE, OWUI, PANEL = CHANNELS["claude-desktop"], CHANNELS["openwebui"], CHANNELS["panel"]
INSTALL = [act("load_context"), act("recheck_fit"), act("build_plan")] + [act("run_step", args={"step": str(n)}) for n in range(1, 7)]
UNDO = [act("undo_step", args={"step": str(n)}) for n in range(6, 0, -1)]


@pytest.fixture(autouse=True)
def os_dialog_exists(monkeypatch):
    monkeypatch.setattr(H.osconfirm, "available", lambda *a, **k: {"available": True, "mechanism": "fake_dialog"})


def run(coro):
    return asyncio.run(coro)


UNDO_ALL = [act("load_setup")] + [act("undo_step", args={"step": str(n)}) for n in range(6, 0, -1)] + [act("finish_undo", args={"summary": "everything the setup wrote was undone"})]
FINISHED = INSTALL + [act("finish_setup", args={"summary": "installed and answered"})]


async def world(tmp_path, script, *, web=False, start=True, undo_script=None):
    """A coordinator, a paired and analysed node, the owner's recorded pick and a setup run driven by `script`. Returns everything a test needs."""
    origins = []
    cm = coordinator(tmp_path / "c", web=web)
    c = await cm.__aenter__()
    agent, task, nid = await node_up(c, tmp_path)
    await analysed(c, nid)
    facts = {k: row["value"] for k, row in c.runs.stored_analysis(nid)["facts"].items()}
    catalog, recipes = make_world(lambda f: origins.append(Origin(f)) or origins[-1], facts)
    record_pick(c, nid, catalog)
    agent.runner.ctx.update({"recipes": recipes})
    orig_exit = cm.__aexit__

    async def close_world(*exc):  # a setup that is left on the machine at the end of a test leaves its server running: the test stops it (the node's own stop, by pid and start time), whatever else happened
        try:
            info = json.loads((agent.state.dir / "mas" / "run" / "server.json").read_text())
            procs.stop(info["pid"], info["created"])
        except (OSError, ValueError, KeyError):
            pass
        for o in origins:  # the stand-in download servers give their listening sockets back
            o.close()
        return await orig_exit(*exc)
    cm.__aexit__ = close_world
    if undo_script is not None:  # the undo run is started later by the chat: the scripted model goes in where the coordinator would pick its own
        orig_undo = c.runs.start_uninstall
        c.runs.start_uninstall = lambda node_id, via="web", **kw: orig_undo(node_id, via, model=ScriptedModel(list(undo_script)), status=AVAILABLE)
    if not start:  # the chat starts it (the Pipe's "Prepare"): the scripted model and the test world go in where the coordinator would pick its own
        orig = c.runs.start_install
        c.runs.start_install = lambda node_id, via="web", **kw: orig(node_id, via, model=ScriptedModel(script), status=AVAILABLE, recipes=recipes, catalog=catalog, rules=LOOSE)
        return cm, c, agent, nid, None
    r = c.runs.start_install(nid, "chat:claude-desktop", model=ScriptedModel(script), status=AVAILABLE, recipes=recipes, catalog=catalog, rules=LOOSE)
    assert r.task is not None, (r.status, r.reason)
    return cm, c, agent, nid, r


async def until(pred, timeout=30.0):
    for _ in range(int(timeout / 0.02)):
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("never happened")


def pending_approval(r):
    p = r.adapter.pending
    return p if p is not None and p.qid.startswith("approve_") else None


def confirm_intent(c, ch, r):
    for i in c.harness.intents(ch, r.id)["intents"]:
        if i["kind"] == "confirm" and i.get("state") == "pending":
            return i
    return None


async def finish(cm, c, r):
    r.task.cancel()
    try:
        await r.task
    except BaseException:  # the run was stopped by the test
        pass
    await cm.__aexit__(None, None, None)


# ---- who may approve a step that writes ------------------------------------------------------------------------------------------------------------

def test_an_approval_that_only_says_owner_is_refused_and_the_step_stays_open_until_a_human_gesture(tmp_path):
    async def go():
        cm, c, agent, nid, r = await world(tmp_path, INSTALL)
        try:
            await until(lambda: pending_approval(r) is not None)
            qid = pending_approval(r).qid
            assert qid == "approve_run_1"
            assert c.answer_run(r.id, qid, "approve") is False  # the default of every direct call used to count as the owner
            assert c.answer_run(r.id, qid, "approve", "owner", "claude-desktop") is False  # nor does naming a channel
            assert c.answer_run(r.id, qid, "approve", "owner", None, "made-up-gesture") is False
            assert pending_approval(r) is not None and pending_approval(r).qid == qid  # still waiting for a person
            assert len(c.log.events("setup_step_approved")) == 0 and tree(agent.state.dir / "mas") == []
            refused = [e.payload for e in c.log.events("owner_authority_refused")]
            assert len(refused) == 3 and all(p["question"] == qid for p in refused)
            assert c.answer_run(r.id, qid, "approve", "owner", None, "panel") is True  # the owner's panel is a gesture
            await until(lambda: len(c.log.events("setup_step_approved")) == 1)
            assert c.log.events("setup_step_approved")[0].effective_tag == "OWNER"
        finally:
            await finish(cm, c, r)
    run(go())


def test_a_chat_cannot_approve_a_setup_step_by_any_door_it_has(tmp_path):
    async def go():
        cm, c, agent, nid, r = await world(tmp_path, INSTALL, web=True)
        try:
            await until(lambda: pending_approval(r) is not None)
            qid = pending_approval(r).qid
            i = confirm_intent(c, CLAUDE, r)
            assert i is not None and i["kind"] == "confirm" and i["payload"]["writes"] is True
            tokens = c.harness.tokens
            # 1. the decision door of the core: a write is never decided by a tool call
            with pytest.raises(Refused) as exc:
                c.harness.decide(CLAUDE, i["id"], "approve")
            assert exc.value.reason == "confirmation_required"
            for ch in ("claude-desktop", "openwebui"):
                st, body = await asyncio.to_thread(call, c, "POST", "/harness/v1/decisions", token=tokens[ch], body={"intent_id": i["id"], "option": "approve"})
                assert st == 409 and body["error"] == "confirmation_required"
            # 2. the owner's own door, with a chat's token and no session: the chat's token opens only /harness/v1
            for ch in ("claude-desktop", "openwebui"):
                st, _ = await asyncio.to_thread(call, c, "POST", "/api/run/answer", token=tokens[ch], body={"run": r.id, "question": qid, "option": "approve"})
                assert st in (401, 403)
                st, _ = await asyncio.to_thread(call, c, "POST", "/api/run/answer", token=tokens[ch], body={"run": r.id, "question": qid, "option": "approve", "by": "owner"}, headers={"Origin": f"http://127.0.0.1:{c.web.port}"})
                assert st in (401, 403)
            # 3. a confirmation the chat cannot press: the host offered no elicitation, and an OS dialog is answered by the operating system, never by the caller
            with pytest.raises(Refused) as exc:
                c.harness.begin_confirmation(CLAUDE, i["id"], "host_elicitation")
            assert exc.value.reason == "mechanism_unavailable"
            c.harness.native_confirm = Dialog("approved", block=True)
            b = c.harness.begin_confirmation(CLAUDE, i["id"], "os_dialog")
            with pytest.raises(Refused) as exc:
                c.harness.complete_confirmation(CLAUDE, b["ticket"], b["plan_hash"], "accept")
            assert exc.value.reason == "not_host_answered"
            # 4. the other chat's ticket is not this chat's
            with pytest.raises(Refused) as exc:
                c.harness.complete_confirmation(OWUI, b["ticket"], b["plan_hash"], "accept")
            assert exc.value.reason == "unknown_ticket"
            assert pending_approval(r) is not None and len(c.log.events("setup_step_approved")) == 0 and tree(agent.state.dir / "mas") == []
            c.harness.native_confirm.release.set()
            await until(lambda: len(c.log.events("setup_step_approved")) == 1)  # only the dialog's answer moves it
        finally:
            await finish(cm, c, r)
    run(go())


def test_the_operator_answering_for_the_owner_never_runs_a_step(tmp_path):
    async def go():
        cm, c, agent, nid, r = await world(tmp_path, INSTALL)
        try:
            await until(lambda: pending_approval(r) is not None)
            assert c.answer_run(r.id, "approve_run_1", "approve", "claude-code") is True  # recorded, as the operator's
            await until(lambda: any(e.type == "setup_step" and e.payload.get("state") == "rejected" for e in r.store.events()))
            assert c.log.events("setup_step_approved")[0].effective_tag == "CLAUDE-CODE"
            assert tree(agent.state.dir / "mas") == []  # nothing was written: the task desk counts only an OWNER approval
        finally:
            await finish(cm, c, r)
    run(go())


def test_a_dialog_the_owner_declines_runs_nothing_and_a_ticket_for_an_old_question_cannot_answer_the_next(tmp_path):
    async def go():
        cm, c, agent, nid, r = await world(tmp_path, INSTALL)
        try:
            await until(lambda: pending_approval(r) is not None)
            first = confirm_intent(c, CLAUDE, r)
            c.harness.native_confirm = Dialog("declined")
            b = c.harness.begin_confirmation(CLAUDE, first["id"], "os_dialog")
            await until(lambda: c.harness.ticket(CLAUDE, b["ticket"])["state"] != "waiting")
            assert c.harness.ticket(CLAUDE, b["ticket"])["state"] == "declined"
            await until(lambda: any(e.type == "setup_step" and e.payload.get("state") == "rejected" or e.type == "owner_answer" for e in r.store.events()))
            assert tree(agent.state.dir / "mas") == [] and len(c.log.events("setup_step_approved")) == 0
            assert [e.payload for e in c.log.events("owner_confirmation")][-1]["approved"] is False
        finally:
            await finish(cm, c, r)
    run(go())


def test_a_host_dialog_answer_for_the_wrong_plan_hash_is_rejected_and_the_right_one_runs_the_step(tmp_path):
    async def go():
        cm, c, agent, nid, r = await world(tmp_path, INSTALL)
        try:
            await until(lambda: pending_approval(r) is not None)
            c.harness.declare(OWUI, {"event_call": True})
            i = confirm_intent(c, OWUI, r)
            b = c.harness.begin_confirmation(OWUI, i["id"], "host_dialog")
            with pytest.raises(Refused) as exc:
                c.harness.complete_confirmation(OWUI, b["ticket"], "sha256:" + "00" * 32, "accept")
            assert exc.value.reason == "plan_hash_mismatch"
            assert len(c.log.events("setup_step_approved")) == 0
            b = c.harness.begin_confirmation(OWUI, i["id"], "host_dialog")
            assert c.harness.complete_confirmation(OWUI, b["ticket"], b["plan_hash"], "accept")["state"] == "approved"
            await until(lambda: len(c.log.events("setup_step_approved")) == 1)
            assert c.log.events("setup_step_approved")[0].payload["plan_hash"] == b["plan_hash"]
        finally:
            await finish(cm, c, r)
    run(go())


def test_preparing_a_machine_with_no_pick_says_why_in_words_and_starts_nothing(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c") as c:
            agent, task, nid = await node_up(c, tmp_path)
            await analysed(c, nid)
            view = c.harness.start(CLAUDE, "prepare", nid)
            assert view["status"] == "no_pick"
            out = c.harness.intents(CLAUDE, view["id"])
            assert out["run"]["finished"] is True
            txt = " ".join(i["text_fallback"] for i in out["intents"])
            assert "it could not start" in txt.lower() and "pick an option for that machine first" in txt.lower()
            with pytest.raises(Refused):
                c.harness.start(CLAUDE, "prepare", "not-a-node")
    run(go())


# ---- the whole flow, as the owner reads it -----------------------------------------------------------------------------------------------------------

def test_pick_plan_text_each_step_through_the_dialog_progress_minimal_call_and_undo_with_its_own_approvals(tmp_path):
    async def go():
        cm, c, agent, nid, r = await world(tmp_path, INSTALL + UNDO + [act("finish_setup", args={"summary": "installed, answered and undone"})])
        report = {"prompts": [], "texts": [], "approvals": []}
        try:
            dialog = Dialog("approved")
            c.harness.native_confirm = dialog
            since = 0
            while not r.task.done():
                out = c.harness.intents(CLAUDE, r.id, since)
                for i in out["intents"]:
                    since = max(since, i["seq"])
                    report["texts"].append((i["kind"], i["actor"], i["text_fallback"]))
                    if i["kind"] == "confirm" and i.get("state") == "pending":
                        n0 = len(dialog.calls)
                        b = c.harness.begin_confirmation(CLAUDE, i["id"], "os_dialog")
                        await until(lambda: c.harness.ticket(CLAUDE, b["ticket"])["state"] != "waiting", 20)
                        assert c.harness.ticket(CLAUDE, b["ticket"])["state"] == "approved"
                        report["prompts"].append(dialog.calls[n0])
                        report["approvals"].append((i["payload"]["action"], i["payload"]["step"], b["plan_hash"]))
                await asyncio.sleep(0.05)
            await r.task
            out = c.harness.intents(CLAUDE, r.id, since)
            report["texts"] += [(i["kind"], i["actor"], i["text_fallback"]) for i in out["intents"]]
            assert r.status == "rolled_back", (r.status, r.reason, r.summary)

            texts = [t for _, _, t in report["texts"]]
            kinds = [k for k, _, _ in report["texts"]]
            # the plan comes first, as data and as words, before any approval is asked
            first_plan = next(n for n, (k, _, t) in enumerate(report["texts"]) if "Setup plan for" in t)
            first_confirm = kinds.index("confirm")
            assert first_plan < first_confirm
            plan_txt = report["texts"][first_plan][2]
            view = c.runs.view(r.id)
            for s in view["plan"]["steps"]:
                assert D(s["command"]) in plan_txt and D(s["undo"]) in plan_txt and f"Step {s['n']} of 6" in plan_txt
            assert view["plan"]["hash"] in plan_txt and "cannot approve or undo anything" in plan_txt and "Undoing is its own request" in plan_txt
            # 6 steps to run and 6 to undo: twelve approvals, each of the one plan, each by the dialog, each its own ticket
            assert [(a, n) for a, n, _ in report["approvals"]] == [("run", n) for n in range(1, 7)] + [("undo", n) for n in range(6, 0, -1)]
            assert {h for _, _, h in report["approvals"]} == {view["plan"]["hash"]}
            assert len(dialog.calls) == 12 and all(view["plan"]["hash"] in m for _, m in dialog.calls)
            title, message = report["prompts"][0]
            assert "approve a step" in title.lower() and "Run step 1 of 6" in message and D(view["plan"]["steps"][0]["command"]) in message and D(view["plan"]["steps"][0]["undo"]) in message
            assert any("Undo step 6 of 6" in m for _, m in report["prompts"])
            # live progress: the machine, the step, what is happening, and who did it
            prog = [(a, t) for k, a, t in report["texts"] if k == "progress"]
            assert any(a == "GUARD" and "sent to the machine" in t and "step 2 of 6" in t and view["node"] in t for a, t in prog)
            assert any(a == "OWNER" and "answered approve_run_2" in t for a, t in prog) and any("approved by you" in t for _, t in prog) and any(a == "MODEL" for a, _ in prog)
            assert any("done" in t for _, t in prog) and any("undoing" in t for _, t in prog)
            mc = next(t for _, t in prog if "minimal call" in t)
            assert re.search(r"latency \d+ ms", mc) and "answer «ready»" in mc and "vision untested" in mc and "None" not in mc  # (the test runtime reports no tokens per second: the line leaves the figure out, it never prints None)
            assert tree(agent.state.dir / "mas") == []  # undone: not one file left
            tags = view["actors"]
            assert tags["CLAUDE-CODE"] == 0 and tags["OWNER"] >= 12 and tags["MODEL"] > 0
            assert len(c.log.events("owner_authority_refused")) == 0  # every approval came with its gesture
            owner_conf = [e.payload for e in c.log.events("owner_confirmation")]
            assert len(owner_conf) == 12 and all(p["mechanism"] == "os_dialog" and p["approved"] for p in owner_conf)
            print("REPORT_PLAN_TEXT\n" + plan_txt)
            print("REPORT_DIALOG_TITLE " + title + "\nREPORT_DIALOG_MESSAGE\n" + message)
            print("REPORT_UNDO_DIALOG\n" + next(m for _, m in report["prompts"] if "Undo step 6" in m))
            print("REPORT_PROGRESS\n" + "\n".join(f"[{a}] {t}" for a, t in prog))
            print("REPORT_ACTORS " + json.dumps(tags))
        finally:
            await cm.__aexit__(None, None, None)
    run(go())


def test_open_webui_prepares_a_machine_with_its_own_dialogs_one_step_at_a_time_and_reports_progress_with_actors(tmp_path):
    async def go():
        cm, c, agent, nid, _ = await world(tmp_path, INSTALL + [act("finish_setup", args={"summary": "installed and answered"})], web=True, start=False)
        try:
            chat = Chat(["4"] + [True] * 6)  # menu: 1 network, 2 analyze, 3 options, 4 prepare; then OK in the chat's own confirmation dialog for each of the 6 steps
            out = await load_pipe(c.state.parent).pipe({"messages": []}, {}, chat.emit, chat.call)
            menu = chat.dialogs[0]["data"]["message"]
            assert "4. Prepare node1" in menu
            run_ = next(r for r in c.runs.runs.values() if r.kind == "install")
            assert run_.status == "done", (run_.status, run_.reason)
            confs = [d["data"] for d in chat.dialogs if d["type"] == "confirmation"]
            view = c.runs.view(run_.id)
            assert len(confs) == 6 and all(view["plan"]["hash"] in d["message"] for d in confs)
            assert all(D(view["plan"]["steps"][n]["command"]) in confs[n]["message"] and f"step {n + 1} of 6" in confs[n]["message"] for n in range(6))
            st = [e["data"]["description"] for e in chat.events if e["type"] == "status"]
            assert any(x.startswith("GUARD:") and "node1" in x and "step 5 of 6" in x for x in st) and any(x.startswith("OWNER:") and "answered approve_run_5" in x for x in st) and st[-1] == "Done"
            assert "Setup plan for" in out and view["plan"]["hash"] in out
            conf = [e.payload for e in c.log.events("owner_confirmation")]
            assert len(conf) == 6 and all(p["channel"] == "openwebui" and p["mechanism"] == "host_dialog" and p["approved"] for p in conf)
            assert view["actors"]["CLAUDE-CODE"] == 0 and len(c.log.events("owner_authority_refused")) == 0
        finally:
            await cm.__aexit__(None, None, None)
    run(go())


def test_claude_desktop_prepares_a_machine_the_model_relays_and_only_the_system_dialog_approves(tmp_path):
    async def go():
        cm, c, agent, nid, _ = await world(tmp_path, INSTALL + [act("finish_setup", args={"summary": "installed and answered"})], web=True, start=False)
        host = FakeHost(c, tmp_path, caps={"extensions": {"io.modelcontextprotocol/ui": {"mimeTypes": ["text/html;profile=mcp-app"]}}})  # the real host's handshake: cards, no elicitation
        try:
            await host.start()
            dialog = Dialog("approved")
            c.harness.native_confirm = dialog
            res = await host.tool("mas_prepare", node="node1")
            run_id = res["structuredContent"]["run_id"]
            texts, refused_by_tool, approvals = [res["content"][0]["text"]], None, 0
            for _ in range(60):
                intents = res["structuredContent"]["intents"]
                conf = [i for i in intents if i["kind"] == "confirm"]
                if conf:
                    if refused_by_tool is None:  # the model tries to approve with the answer tool: it is refused, and nothing moves
                        refused_by_tool = await host.tool("mas_answer", intent_id=conf[0]["id"], option="approve")
                        assert len(c.log.events("setup_step_approved")) == approvals
                    out = await host.tool("mas_request_approval", intent_id=conf[0]["id"])
                    texts.append(out["content"][0]["text"])
                    assert "system dialog" in out["content"][0]["text"] and out["structuredContent"]["state"] == "approved"
                    approvals += 1
                if res["structuredContent"].get("status") in ("done", "rolled_back", "stopped", "blocked_needs_human", "cannot_install"):
                    break
                res = await host.tool("mas_status", run_id=run_id, wait_s=2)
                texts.append(res["content"][0]["text"])
            assert c.runs.runs[run_id].status == "done", (c.runs.runs[run_id].status, c.runs.runs[run_id].reason)
            assert refused_by_tool["isError"] is True and "confirmation_required" in refused_by_tool["content"][0]["text"]
            assert approvals == 6 and len(dialog.calls) == 6 and len(c.log.events("setup_step_approved")) == 6 and len(c.log.events("owner_authority_refused")) == 0
            alltext = "\n".join(texts)
            view = c.runs.view(run_id)
            assert "Setup plan for" in alltext and view["plan"]["hash"] in alltext and all(D(st["command"]) in alltext for st in view["plan"]["steps"])
            assert "mas_request_approval" in alltext and "approved by you" in alltext and "node1 · minimal call" in alltext
            assert "<svg" not in alltext and "data-card" not in alltext  # what the chat's model reads is words; the card HTML stays out of its reach
            assert all(c.harness.ticket(CLAUDE, t)["assurance"] in ("strong", "weak", "") for t in list(c.harness._tickets) if c.harness._tickets[t].channel == "claude-desktop")
            assert view["actors"]["CLAUDE-CODE"] == 0
        finally:
            await host.stop()
            await cm.__aexit__(None, None, None)
    run(go())


# ---- undoing a finished setup, later, in a new run ---------------------------------------------------------------------------------------------------

async def approve_with_panel(c, r, upto=None):
    """The owner presses the panel's button for every step the run asks about (SIMULATED: the panel's gesture), until the run ends."""
    while not r.task.done():
        p = pending_approval(r)
        if p is not None:
            c.answer_run(r.id, p.qid, "approve", "owner", None, "panel")
        await asyncio.sleep(0.02)
    await r.task


async def installed(tmp_path, *, script=FINISHED, undo_script=UNDO_ALL, web=False):
    cm, c, agent, nid, r = await world(tmp_path, script, web=web, undo_script=undo_script)
    await approve_with_panel(c, r)
    return cm, c, agent, nid, r


def start_undo(c, nid, ch=CLAUDE):
    view = c.harness.start(ch, "undo", nid)
    return view, c.runs.runs[view["id"]]


def test_a_finished_setup_is_undone_later_in_a_new_run_from_the_record_plan_first_each_step_by_a_dialog(tmp_path):
    async def go():
        cm, c, agent, nid, r = await installed(tmp_path)
        try:
            assert r.status == "done"
            rec = c.runs.stored_setup(nid)
            assert rec is not None and rec["run"] == r.id and rec["plan"]["plan_hash"] == c.runs.view(r.id)["plan"]["hash"] and set(rec["status"].values()) == {"done"}
            assert any(f.startswith("runtime") for f in tree(agent.state.dir / "mas"))  # the machine has the setup on it
            view, u = start_undo(c, nid)
            assert u.kind == "uninstall" and u.id != r.id
            dialog = Dialog("approved")
            c.harness.native_confirm = dialog
            texts, approvals, since = [], [], 0
            while not u.task.done():
                out = c.harness.intents(CLAUDE, u.id, since)
                for i in out["intents"]:
                    since = max(since, i["seq"])
                    texts.append((i["kind"], i["actor"], i["text_fallback"]))
                    if i["kind"] == "confirm" and i.get("state") == "pending":
                        b = c.harness.begin_confirmation(CLAUDE, i["id"], "os_dialog")
                        await until(lambda: c.harness.ticket(CLAUDE, b["ticket"])["state"] != "waiting", 20)
                        assert c.harness.ticket(CLAUDE, b["ticket"])["state"] == "approved"
                        approvals.append((i["payload"]["action"], i["payload"]["step"]))
                await asyncio.sleep(0.05)
            await u.task
            texts += [(i["kind"], i["actor"], i["text_fallback"]) for i in c.harness.intents(CLAUDE, u.id, since)["intents"]]
            assert u.status == "rolled_back", (u.status, u.reason)
            kinds = [k for k, _, _ in texts]
            plan_txt = next(t for k, _, t in texts if "Plan to undo" in t)
            assert [n for n, (k, _, t) in enumerate(texts) if "Plan to undo" in t][0] < kinds.index("confirm")  # the undo plan is told before the first approval
            steps = rec["plan"]["steps"]
            assert all(f"Undo step {x['n']} of 6" in plan_txt and D(x["undo"]) in plan_txt for x in steps)  # what the recorded plan itself wrote as the undo of each step
            assert plan_txt.index("Undo step 6 of 6") < plan_txt.index("Undo step 1 of 6")  # newest first
            assert rec["plan"]["plan_hash"] in plan_txt and "cannot approve anything" in plan_txt and "Only what that plan wrote is undone" in plan_txt and "SmolLM2" in plan_txt.splitlines()[0]
            assert approvals == [("undo", n) for n in range(6, 0, -1)] and len(dialog.calls) == 6
            assert all(rec["plan"]["plan_hash"] in m and "What is undone" in m for _, m in dialog.calls)
            prog = [t for k, _, t in texts if k == "progress"]
            assert any("undoing step 3 of 6" in t and "done" in t and view["node"] in t for t in prog)
            assert tree(agent.state.dir / "mas") == []  # not one file left
            assert c.runs.stored_setup(nid) is None and not (c.runs.setups / f"{nid}.json").exists()  # nothing left to undo, so nothing recorded
            ups = [e for e in c.log.events("setup_step_approved") if e.payload["run"] == u.id]
            assert len(ups) == 6 and all(e.payload["action"] == "undo" and e.payload["plan_hash"] == rec["plan"]["plan_hash"] and e.effective_tag == "OWNER" for e in ups)
            assert c.runs.view(u.id)["actors"]["CLAUDE-CODE"] == 0 and len(c.log.events("owner_authority_refused")) == 0
            print("REPORT_UNDO_PLAN_TEXT\n" + plan_txt)
        finally:
            await cm.__aexit__(None, None, None)
    run(go())


def test_the_undo_run_has_the_same_authority_rule_no_chat_door_approves_an_undo_step(tmp_path):
    async def go():
        cm, c, agent, nid, r = await installed(tmp_path, web=True)
        try:
            before = tree(agent.state.dir / "mas")
            view, u = start_undo(c, nid)
            await until(lambda: pending_approval(u) is not None)
            qid = pending_approval(u).qid
            assert qid == "approve_undo_6"
            i = confirm_intent(c, CLAUDE, u)
            assert i is not None and i["kind"] == "confirm" and i["payload"]["action"] == "undo" and i["payload"]["writes"] is True
            assert c.answer_run(u.id, qid, "approve") is False and c.answer_run(u.id, qid, "approve", "owner", "claude-desktop") is False and c.answer_run(u.id, qid, "approve", "owner", None, "made-up") is False
            with pytest.raises(Refused) as exc:
                c.harness.decide(CLAUDE, i["id"], "approve")
            assert exc.value.reason == "confirmation_required"
            for ch in ("claude-desktop", "openwebui"):
                st, body = await asyncio.to_thread(call, c, "POST", "/harness/v1/decisions", token=c.harness.tokens[ch], body={"intent_id": i["id"], "option": "approve"})
                assert st == 409 and body["error"] == "confirmation_required"
                st, _ = await asyncio.to_thread(call, c, "POST", "/api/run/answer", token=c.harness.tokens[ch], body={"run": u.id, "question": qid, "option": "approve"})
                assert st in (401, 403)
            with pytest.raises(Refused) as exc:
                c.harness.begin_confirmation(CLAUDE, i["id"], "host_elicitation")
            assert exc.value.reason == "mechanism_unavailable"
            assert pending_approval(u).qid == qid and tree(agent.state.dir / "mas") == before  # still waiting for a person, and nothing was removed
            # the operator answering for the owner is recorded apart and removes nothing
            assert c.answer_run(u.id, qid, "approve", "claude-code") is True
            await until(lambda: any(e.type == "setup_step" and e.payload.get("state") == "rejected" for e in u.store.events()))
            ev = [e for e in c.log.events("setup_step_approved") if e.payload["run"] == u.id]
            assert len(ev) == 1 and ev[0].effective_tag == "CLAUDE-CODE" and tree(agent.state.dir / "mas") == before
        finally:
            await finish(cm, c, u)
    run(go())


def test_a_dialog_the_owner_declines_removes_nothing_and_the_record_stays(tmp_path):
    async def go():
        cm, c, agent, nid, r = await installed(tmp_path)
        try:
            before = tree(agent.state.dir / "mas")
            view, u = start_undo(c, nid)
            await until(lambda: pending_approval(u) is not None)
            c.harness.native_confirm = Dialog("declined")
            b = c.harness.begin_confirmation(CLAUDE, confirm_intent(c, CLAUDE, u)["id"], "os_dialog")
            await until(lambda: c.harness.ticket(CLAUDE, b["ticket"])["state"] != "waiting")
            assert c.harness.ticket(CLAUDE, b["ticket"])["state"] == "declined"
            await until(lambda: any(e.type == "owner_answer" for e in u.store.events()))
            assert tree(agent.state.dir / "mas") == before and c.runs.stored_setup(nid) is not None
        finally:
            await finish(cm, c, u)
    run(go())


def test_only_what_the_plan_wrote_and_only_the_steps_that_are_done_are_offered(tmp_path):
    async def go():
        stopped = [act("load_context"), act("recheck_fit"), act("build_plan")] + [act("run_step", args={"step": str(n)}) for n in range(1, 4)] + [act("finish_setup", args={"summary": "stopped after the model download"})]
        cm, c, agent, nid, r = await installed(tmp_path, script=stopped, undo_script=[act("load_setup"), act("undo_step", args={"step": "3"}), act("undo_step", args={"step": "2"}), act("finish_undo", args={"summary": "two steps undone"})])
        try:
            assert r.status == "blocked_needs_human"  # the setup ended before it was verified (a typed stop, never done): three steps are on the machine
            rec = c.runs.stored_setup(nid)
            assert {int(n): st for n, st in rec["status"].items()} == {1: "done", 2: "done", 3: "done", 4: "pending", 5: "pending", 6: "pending"}
            view, u = start_undo(c, nid)
            await approve_with_panel(c, u)
            plan_txt = next(i["text_fallback"] for i in c.harness.intents(CLAUDE, u.id)["intents"] if "Plan to undo" in i["text_fallback"])
            assert [f"Undo step {n} of 6" in plan_txt for n in range(1, 7)] == [True, True, True, False, False, False]  # steps 4-6 never ran: not offered
            assert u.status == "blocked_needs_human"  # the model finished with step 1 still on the machine: a typed stop that needs the owner, never rolled_back
            left = c.runs.stored_setup(nid)
            assert {int(n): st for n, st in left["status"].items()}[1] == "done" and {int(n): st for n, st in left["status"].items()}[3] == "undone"  # the record says what is still there
            assert c.runs.view(u.id)["plan"]["hash"] == rec["plan"]["plan_hash"]
        finally:
            await cm.__aexit__(None, None, None)
    run(go())


def test_a_machine_with_nothing_set_up_says_so_and_starts_no_run(tmp_path):
    async def go():
        async with coordinator(tmp_path / "c") as c:
            agent, task, nid = await node_up(c, tmp_path)
            view = c.harness.start(CLAUDE, "undo", nid)
            assert view["status"] == "nothing_to_undo" and view["finished"]
            txt = " ".join(i["text_fallback"] for i in c.harness.intents(CLAUDE, view["id"])["intents"])
            assert "It could not start" in txt and "nothing set up to undo" in txt
    run(go())


# ---- the text the owner approves is the command that runs, on every host -------------------------------------------------------------------------------
# A chat may treat an angle-bracket word as an HTML tag and strip it: Open WebUI turned `rm -rf <mas>/downloads/x` into `rm -rf /downloads/x` in an undo dialog, a delete from the disk root. The text of
# every approval now carries `MAS_HOME` for the plan's `<mas>`, and these tests hold each host to: (1) the text a human approves, taken back to the plan's own words, IS the command in the plan the node is sent;
# (2) a host that strips tags changes nothing in it; (3) an undo says "Removes", never "Writes to".

import re as _re


def host_strips(text: str) -> str:
    """What a host that treats `<word>` as a tag shows (Open WebUI's dialogs do)."""
    return _re.sub(r"<[^>]*>", "", text)


def capture_plans(c):
    """Wrap the task desk's door to the node: record the plan, step and action of every step that is really sent."""
    sent = []
    orig = c.tasks.start_setup_step

    def spy(node_id, plan, step, action="run", **kw):
        sent.append((plan, step, action))
        return orig(node_id, plan, step, action, **kw)
    c.tasks.start_setup_step = spy
    return sent


def runs_text(plan, n, action):
    st = plan["steps"][n - 1]
    return st["exact_command"] if action == "run" else st["undo"]


def check_shown(text, plan, n, action, *, where):
    """One approval text, on one host: it holds the plan's command in display form, survives a tag-stripping host, shows the right label for what it does, and maps back to the plan's own words."""
    cmd = runs_text(plan, n, action)
    assert D(cmd) in text, (where, text)
    assert undisplay(D(cmd)) == cmd  # the display form is a pure, reversible function of the plan's text
    assert host_strips(text) == text, (where, "a tag-stripping host would change what the owner reads", text)
    assert "<" not in text and ">" not in text, (where, text)
    if action == "undo":
        assert "Removes:" in text and "Writes to:" not in text, (where, text)
    elif plan["steps"][n - 1]["writes"]:
        assert "Writes to:" in text, (where, text)


def test_the_raw_placeholder_would_have_looked_like_a_delete_from_the_disk_root_on_a_tag_stripping_host():
    raw = "rm -rf <mas>/downloads/llama-b12017-bin-macos-arm64.tar.gz"
    assert host_strips(raw) == "rm -rf /downloads/llama-b12017-bin-macos-arm64.tar.gz"  # the bug, as the owner saw it
    assert host_strips(D(raw)) == D(raw) == "rm -rf MAS_HOME/downloads/llama-b12017-bin-macos-arm64.tar.gz" and undisplay(D(raw)) == raw


def test_a_confirmation_whose_command_holds_angle_brackets_is_never_shown_at_all(tmp_path):
    from envrec_harness.intents import Intent, IntentInvalid, validate
    base = {"writes": True, "summary": "Run step 1", "command": "rm -rf x", "undo": "nothing", "plan_hash": "sha256:" + "ab" * 32}
    validate(Intent(id="i", kind="confirm", run_id=None, actor="GUARD", sensitivity="internal", payload=base, text_fallback="Run step 1"))
    for field in ("command", "undo", "summary"):
        with pytest.raises(IntentInvalid):  # fail closed: what a host would strip is not shown
            validate(Intent(id="i", kind="confirm", run_id=None, actor="GUARD", sensitivity="internal", payload={**base, field: "rm -rf <mas>/downloads/x"}, text_fallback="Run step 1"))


def test_every_approval_text_equals_the_command_that_runs_in_the_chat_text_the_system_dialog_and_the_panel(tmp_path):
    """Claude Desktop's text, the operating system's dialog and the owner's panel, for the six steps of a setup and the six of its undo."""
    async def go():
        cm, c, agent, nid, r = await world(tmp_path, FINISHED, undo_script=UNDO_ALL)
        sent = capture_plans(c)
        try:
            dialog = Dialog("approved")
            c.harness.native_confirm = dialog
            seen = 0

            async def walk(run_, action_expected):
                nonlocal seen
                since = 0
                while not run_.task.done():
                    for i in c.harness.intents(CLAUDE, run_.id, since)["intents"]:
                        since = max(since, i["seq"])
                        if i["kind"] != "confirm" or i.get("state") != "pending":
                            continue
                        n, action = i["payload"]["step"], i["payload"]["action"]
                        assert action == action_expected
                        plan = run_.adapter.plan
                        check_shown(i["text_fallback"], plan, n, action, where="chat text (Claude Desktop)")
                        check_shown("\n".join(str(i["payload"][k]) for k in ("command", "undo")) + "\n" + ("Removes: " if action == "undo" else "Writes to: ") + ", ".join(i["payload"]["targets"]), plan, n, action, where="intent payload")
                        pend = [x for x in c.harness.pending_confirmations() if x["intent"] == i["id"]]
                        assert len(pend) == 1 and pend[0]["action"] == action and pend[0]["command"] == i["payload"]["command"] and pend[0]["undo"] == i["payload"]["undo"]
                        panel = pend[0]["command"] + "\n" + ("Removes: " if action == "undo" else "Writes to: ") + ", ".join(pend[0]["targets"])  # what the panel's script draws (app.js, textContent)
                        check_shown(panel, plan, n, action, where="owner panel")
                        assert host_strips(pend[0]["undo"]) == pend[0]["undo"] and all(host_strips(x) == x for x in pend[0]["targets"])
                        n0 = len(dialog.calls)
                        b = c.harness.begin_confirmation(CLAUDE, i["id"], "os_dialog")
                        await until(lambda: c.harness.ticket(CLAUDE, b["ticket"])["state"] != "waiting", 20)
                        check_shown(dialog.calls[n0][1], plan, n, action, where="system dialog")
                        seen += 1
                    await asyncio.sleep(0.05)
                await run_.task
            await walk(r, "run")
            assert r.status == "done" and seen == 6
            view, u = start_undo(c, nid)
            await walk(u, "undo")
            assert u.status == "rolled_back" and seen == 12
            # the plan the node was sent for each step says what the owner approved (one-to-one, in order)
            assert [(n, a) for _, n, a in sent] == [(n, "run") for n in range(1, 7)] + [(n, "undo") for n in range(6, 0, -1)]
            assert all(plan["plan_hash"] == r.adapter.plan["plan_hash"] for plan, _, _ in sent)
        finally:
            await cm.__aexit__(None, None, None)
    run(go())


def test_open_webui_dialogs_show_the_command_that_runs_for_the_setup_and_for_its_undo_and_survive_a_tag_stripping_host(tmp_path):
    async def go():
        cm, c, agent, nid, _ = await world(tmp_path, FINISHED, web=True, start=False, undo_script=UNDO_ALL)
        sent = capture_plans(c)
        try:
            chat = Chat(["4"] + [True] * 6)
            await load_pipe(c.state.parent).pipe({"messages": []}, {}, chat.emit, chat.call)
            setup_run = next(r for r in c.runs.runs.values() if r.kind == "install")
            plan = setup_run.adapter.plan
            confs = [d["data"]["message"] for d in chat.dialogs if d["type"] == "confirmation"]
            assert len(confs) == 6
            for n, msg in enumerate(confs, 1):
                check_shown(msg, plan, n, "run", where="Open WebUI dialog (setup)")
            chat = Chat(["5"] + [True] * 6)  # menu entry 5: undo what was set up
            await load_pipe(c.state.parent).pipe({"messages": []}, {}, chat.emit, chat.call)
            confs = [d["data"]["message"] for d in chat.dialogs if d["type"] == "confirmation"]
            assert len(confs) == 6
            for n, msg in zip(range(6, 0, -1), confs):
                check_shown(msg, plan, n, "undo", where="Open WebUI dialog (undo)")
            assert [(n, a) for _, n, a in sent] == [(n, "run") for n in range(1, 7)] + [(n, "undo") for n in range(6, 0, -1)]
            assert tree(agent.state.dir / "mas") == []
        finally:
            await cm.__aexit__(None, None, None)
    run(go())


def test_claude_desktops_own_confirmation_form_shows_the_command_that_runs(tmp_path):
    """The host that can show an elicitation form: the message of the form is the core's text, with the same guarantees."""
    async def go():
        cm, c, agent, nid, _ = await world(tmp_path, FINISHED, web=True, start=False)
        sent = capture_plans(c)
        host = FakeHost(c, tmp_path, caps={"elicitation": {"form": {}}, "extensions": {"io.modelcontextprotocol/ui": {"mimeTypes": ["text/html;profile=mcp-app"]}}})
        try:
            await host.start()
            res = await host.tool("mas_prepare", node="node1")
            run_id = res["structuredContent"]["run_id"]
            for _ in range(60):
                conf = [i for i in res["structuredContent"]["intents"] if i["kind"] == "confirm"]
                if conf:
                    await host.tool("mas_request_approval", intent_id=conf[0]["id"])
                if res["structuredContent"].get("status") in ("done", "stopped", "blocked_needs_human", "failed"):
                    break
                res = await host.tool("mas_status", run_id=run_id, wait_s=2)
            plan = c.runs.runs[run_id].adapter.plan
            assert len(host.elicitations) == 6
            for n, params in enumerate(host.elicitations, 1):
                check_shown(params["message"], plan, n, "run", where="Claude Desktop elicitation form")
            assert [(n, a) for _, n, a in sent] == [(n, "run") for n in range(1, 7)]
        finally:
            await host.stop()
            await cm.__aexit__(None, None, None)
    run(go())


def test_the_panel_draws_removes_for_an_undo_and_one_approval_per_question():
    js = (Path(__file__).resolve().parents[1] / "src/envrec_coordinator/web_static/app.js").read_text()
    assert 'c.action === "undo"' in js and '"approvals.removes"' in js and '"approvals.writes"' in js and "[c.intent, c.plan_hash]" in js  # the next step of the same plan is a new approval, not the old one
    strings = json.loads((Path(__file__).resolve().parents[1] / "src/envrec_coordinator/web_static/strings.en.json").read_text())
    assert strings["approvals.removes"] == "Removes" and strings["approvals.writes"] == "Writes to" and strings["approvals.undoing"] == "What is undone"

"""The flagship flow (P1): "analyze the files in a folder of my Lenovo with a model on the Lenovo", asked from a CLOUD chat. The harness, not the chat model, derives the level from the owner's profile, asks
to read the folder, holds the listing, finds no local route, proposes a local model that fits (ONE question), runs the existing setup plan, proposes the measurement a confidential task needs, resumes the
task, and only the model's answer and the route come back. SIMULATED-LOCAL: real coordinator, node over loopback TLS and MCP server; the setup run and the node's model are replaced by their recorded
outcomes; the owner's answers are played by the test, which is the only place that does."""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from acceptance_world import build, owner_yes, set_server
from test_acceptance_run import sc, text

from envrec_coordinator.harness import CHANNELS
from envrec_coordinator.router import EntryChat, Router, Task

CLOUD = CHANNELS["claude-desktop"]
NAMES = ["Quillfeather-merger-draft.docx", "Zanzibar-payroll-2026.xlsx", "holiday-photos"]
SECRET = "SECRET-CONTENT-OF-A-FILE-THE-HARNESS-MUST-NEVER-READ"
FACTS = {"machine.os.name": "Windows 11", "machine.os.arch": "amd64", "machine.model_path": "cpu", "machine.vram_gib": None, "machine.ram_total_gib": 16.0, "machine.disk_free_gib": 200.0,
         "machine.gpus": [{"vendor": "Intel", "memory": "shared"}], "machine.runtimes": [], "machine.ai_clis": [], "machine.api_key_variable_names": [], "analysis.finished": True}


def run(coro):
    return asyncio.run(coro)


def make_folder(tmp):
    d = tmp / "Desk"
    d.mkdir()
    (d / NAMES[0]).write_text(SECRET)
    (d / NAMES[1]).write_bytes(b"0" * 3000)
    (d / NAMES[2]).mkdir()
    return d


def store_analysis(c, nid):
    c.runs.analyses.mkdir(parents=True, exist_ok=True)
    (c.runs.analyses / f"{nid}.json").write_text(json.dumps({"run": "a1", "node": nid, "stored_at": time.time(), "facts": {k: {"value": v, "status": "tested", "probe": "x"} for k, v in FACTS.items()}}))


def pending(c, base):
    return [i for log in c.harness._logs.values() for i in log if i.kind == "ask_choice" and i.extra.get("state") == "pending" and str(i.extra.get("base", "")).startswith(base)]


async def until(c, cid, states, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        j = c.services.jobs[cid]
        if j.state in states:
            return j
        await asyncio.sleep(0.05)
    raise AssertionError(f"chain {cid} is in {c.services.jobs[cid].state!r}: {c.services.jobs[cid].text[:300]}")


class Seen:
    """Everything the CHAT is shown: every tool result (text and structured data) and every reply to one of its answers."""

    def __init__(self):
        self.parts: list[str] = []

    def tool_result(self, r):
        self.parts.append(text(r) + json.dumps(sc(r), default=str))
        return r

    def reply(self, d):
        self.parts.append(json.dumps(d, default=str))
        return d

    def blob(self):
        return "\n".join(self.parts)


def fake_install(c, nid, calls):
    def start_install(node_id, via="web", **kw):
        calls.append((node_id, via, c.runs.stored_pick(node_id)))
        set_server(c, node_id, up=True)  # what a finished setup leaves: the pick and the plan whose start step is done
        fut = asyncio.get_event_loop().create_future()
        fut.set_result(None)
        return SimpleNamespace(id="inst1", status="done", reason="", task=fut)
    c.runs.start_install = start_install


async def flow(tmp_path):
    w, c, nid, host, teardown = await build(tmp_path, key=False, server=False)
    return w, c, nid, host, teardown


def test_the_whole_chain_from_a_cloud_chat_the_listing_never_reaches_it_and_the_answer_does_with_names_masked(tmp_path):
    async def go():
        w, c, nid, host, teardown = await flow(tmp_path)
        try:
            d = make_folder(tmp_path)
            c.services.profile.set("confidential", ["personal"]); c.services.profile.apply(c.services.policy)
            store_analysis(c, nid)
            calls: list = []
            fake_install(c, nid, calls)
            w.script.accuracy[("llamacpp-local", "local-hardware", "text")] = 10
            w.script.answers.append((lambda e, a, p: e == "llamacpp-local" and "Contents of the folder" in p,
                                     lambda e, a, p: f"Mostly work files: {NAMES[0]} looks like a draft, {NAMES[1].upper()} a spreadsheet, and a folder called {NAMES[2]}."))
            seen = Seen()

            # 1. the cloud chat asks; the harness asks the owner ONE question about the folder (a cloud chat cannot create a confidential task from its own words)
            r = seen.tool_result(await host.tool("mas_create_task", text="Analyze the files on my Lenovo's Desktop", task_type="file analysis", touches=["none"],
                                                 inputs=[{"kind": "folder", "machine": "lenovo", "folder": str(d)}]))
            assert sc(r)["ok"] is True and sc(r)["question"] is True and str(d) in text(r) and "this chat never gets it" in text(r) and "never inside a file" in text(r)
            cid = sc(r)["chain"]
            assert not c.services.tasks and not c.services.held  # nothing was read before his yes
            (q,) = pending(c, f"chain:{cid}:in")
            assert [o["id"] for o in q.payload["options"]] == ["approve", "decline"]

            # 2. his yes: the listing is read, held, and the router finds no local route; the harness proposes a local model
            seen.reply(c.harness.decide(CLOUD, q.id, "approve"))
            j = await until(c, cid, ("awaiting_answer",))
            assert "Recommended:" in j.text and "Runner-up:" in j.text and "never leaves your computers" in j.text and "confidential" in j.text
            ev = [e.payload for e in c.log.events("input_read")]
            assert ev and ev[0]["entries"] == 3 and "held" in ev[0] and not any(n in json.dumps(ev) for n in NAMES)  # counts only in the log
            (held,) = c.services.held.values()
            assert all(n in held["text"] for n in NAMES) and SECRET not in held["text"]  # the harness holds the names; no file was opened
            (sq,) = pending(c, f"chain:{cid}:setup")
            assert [o["id"] for o in sq.payload["options"]] == ["use_default", "use_runner", "neither"]
            seen.parts.append(j.text)

            # 3. his pick: recorded as his, the existing setup run starts from it
            reply = seen.reply({"next": c.harness.decide(CLOUD, sq.id, "use_default")})
            assert calls and calls[0][0] == nid and calls[0][2]["option"]["origin"] == "catalog_runtime"
            assert [e.payload["option"] for e in c.log.events("setup_option_picked") if e.payload.get("chain") == cid][0] == calls[0][2]["option"]["id"]

            # 4. the model is there but not measured: a confidential task waits for a measurement; the harness proposes the exam
            j = await until(c, cid, ("awaiting_approval",))
            assert "measurement" in j.text and "confidential" in j.text and j.intent_id
            seen.parts.append(j.text)
            seen.tool_result(await owner_yes(host, j.intent_id))  # the owner's gesture on the exam (played by this world's dialog)

            # 5. measured: the chain creates the task; the owner approves the exact route as for any task
            j = await until(c, cid, ("task_created",), timeout=60)
            seen.parts.append(j.text)
            tid = j.data["task_id"]
            assert "llamacpp-local" in j.text and "Quillfeather" not in j.text
            task = c.services.tasks[tid]["task"]
            assert task.sensitivity == "confidential" and task.held_inputs and task.entry_chat.kind == "cloud" and task.exclude is None
            seen.tool_result(await owner_yes(host, c.services.tasks[tid]["intent_id"]))
            from acceptance_world import wait_job
            done = seen.tool_result(await wait_job(host, tid, until=("done_unchecked", "done", "failed", "refused"), timeout=60))
            out = text(done)

            # 6. the chat sees the answer and the route, with every listing name masked, and nothing else from the folder
            assert "Answer from llamacpp-local" in out and "[name withheld]" in out and "looks like a draft" in out
            blob = seen.blob()
            for n in NAMES + [SECRET]:  # the folder path appears: the chat gave it itself
                assert n.lower() not in blob.lower(), n
            assert str(tmp_path) not in blob.replace(str(d), "")  # only the path the chat itself gave appears (the listing has no other path)
            assert sc(done)["state"] in ("done_unchecked", "done")
            from collections import Counter
            tags = Counter(e.effective_tag for e in c.log.events())
            assert tags.get("CLAUDE-CODE", 0) == 0 and tags["OWNER"] >= 4 and tags["ROUTER"] >= 1 and tags["GUARD"] > tags["OWNER"]  # the owner's answers (folder, setup pick, exam, route) are OWNER; the rule's choice is ROUTER; nothing is the operator's
        finally:
            await teardown()
    run(go())


def test_a_cloud_chat_that_itself_says_the_task_is_confidential_is_still_refused(tmp_path):
    async def go():
        w, c, nid, host, teardown = await flow(tmp_path)
        try:
            d = make_folder(tmp_path)
            c.services.profile.set("confidential", ["personal"]); c.services.profile.apply(c.services.policy)
            r = await host.tool("mas_create_task", text="Summarise this client's personal record", task_type="summarize", touches=["personal"], inputs=[{"kind": "folder", "machine": "lenovo", "folder": str(d)}])
            assert sc(r)["ok"] is False and "owner panel or a local chat" in text(r) and not c.services.held and not c.services.chains
            router = c.services.router.explain(Task(task_id="x", task_type="text", sensitivity="confidential", entry_chat=EntryChat(kind="cloud", name="c"), created_by="OWNER", payload_ref="r"))
            assert router.code == "confidential_from_cloud_chat"  # and the router, asked without harness-held inputs, still refuses
        finally:
            await teardown()
    run(go())


def test_declining_reads_nothing_and_creates_nothing_and_a_bad_input_is_answered_before_anything_is_asked(tmp_path):
    async def go():
        w, c, nid, host, teardown = await flow(tmp_path)
        try:
            d = make_folder(tmp_path)
            c.services.profile.set("confidential", ["personal"]); c.services.profile.apply(c.services.policy)
            for bad in ([{"kind": "drive", "machine": "lenovo", "folder": "x"}], [{"kind": "folder", "machine": "nowhere", "folder": "x"}], [{"kind": "folder", "machine": "lenovo"}], []):
                r = await host.tool("mas_create_task", text="Analyze", task_type="file analysis", touches=["none"], inputs=bad)
                assert not c.services.chains or sc(r).get("chain") is None
            r = await host.tool("mas_create_task", text="Analyze", task_type="file analysis", touches=["none"], inputs=[{"kind": "folder", "machine": "lenovo", "folder": str(d)}])
            (q,) = pending(c, f"chain:{sc(r)['chain']}:in")
            reply = c.harness.decide(CLOUD, q.id, "decline")
            assert "did not approve" in c.services.jobs[sc(r)["chain"]].text and not c.services.held and not c.services.tasks
            assert [e.payload["option"] for e in c.log.events("input_answered")] == ["decline"] and c.log.events("input_answered")[0].effective_tag == "OWNER"
        finally:
            await teardown()
    run(go())


def test_a_folder_the_computer_cannot_read_ends_the_chain_with_what_to_say_instead_and_nothing_is_created(tmp_path):
    async def go():
        w, c, nid, host, teardown = await flow(tmp_path)
        try:
            c.services.profile.set("confidential", ["personal"]); c.services.profile.apply(c.services.policy)
            r = await host.tool("mas_create_task", text="Analyze", task_type="file analysis", touches=["none"], inputs=[{"kind": "folder", "machine": "lenovo", "folder": "Nowhere"}])
            (q,) = pending(c, f"chain:{sc(r)['chain']}:in")
            c.harness.decide(CLOUD, q.id, "approve")
            j = await until(c, sc(r)["chain"], ("failed",))
            assert "not a folder name the computer knows" in j.text and "desktop, documents, downloads, pictures" in j.text and "absolute path" in j.text  # the names come from the os-folders data
            assert not c.services.tasks and not c.services.held
        finally:
            await teardown()
    run(go())

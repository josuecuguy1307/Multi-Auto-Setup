"""The live activity of the machines, in the owner's chat: one line per thing that happened, naming the machine, what happened and who did it; merged and rate-limited so a chat is not flooded; the end of a run
and every decision always shown; the same stream for Claude Desktop (a tool the model calls), Open WebUI (its status line) and the owner's panel.
SIMULATED-LOCAL: a real coordinator and a real node on loopback, a scripted model; the hosts are played by the tests."""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

from envrec.model import ScriptedModel
from envrec_coordinator import harness as H
from envrec_coordinator.harness import CHANNELS, FEED_MAX_LINES
from mas_world import coordinator
from test_harness_mcp import FakeHost
from test_harness_openwebui import Chat, load_pipe, scripted
from test_mas_agent import ANALYSIS_SCRIPT, AVAILABLE, down, node_up

CLAUDE, OWUI = CHANNELS["claude-desktop"], CHANNELS["openwebui"]
ROOT = Path(__file__).resolve().parents[1]


def run(coro):
    return asyncio.run(coro)


async def settle(pred, timeout=20.0):
    for _ in range(int(timeout / 0.02)):
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("never happened")


async def follow(core, r, ch, *, poll=0.004):
    """A chat that keeps asking what happened while the run goes (as the hosts do): returns every line it was told, in order, and the sequence number it ended on."""
    got, since = [], 0
    while True:
        done = r.task.done()
        out = core.activity(ch, since)
        got += out["intents"]
        since = out["seq"]
        if done:
            return got, since
        await asyncio.sleep(poll)


def fake_run(core, rid="r1", name="Lenovo", finished=None):
    return SimpleNamespace(id=rid, node_name=name, finished=finished, kind="analysis", status="running")


def lines_of(core, texts, important=()):
    return [{"actor": a, "text": f"Lenovo · {x}", "important": x in important, "short": None} for a, x in texts]


# ---- merging ------------------------------------------------------------------------------------------------------------------------------------------

def test_a_tool_call_is_one_line_with_the_machine_the_tool_the_outcome_and_who_chose_and_ran_it(tmp_path):
    async def go():
        async with coordinator(tmp_path, web=True) as c:
            agent, task, nid = await node_up(c, tmp_path)
            c.harness.feed_gap_s = 0
            try:
                r = c.runs.start_analysis(nid, "chat:claude-desktop", model=ScriptedModel(list(ANALYSIS_SCRIPT)), status=AVAILABLE)
                got, _ = await follow(c.harness, r, CLAUDE)
                texts = [(i["actor"], i["text_fallback"]) for i in got]
                tool_lines = [x for x in texts if "chosen by" in x[1]]
                assert tool_lines, texts
                actor, text = tool_lines[0]
                assert actor == "MODEL" and text.startswith("node1 · ") and "chosen by MODEL, run by GUARD" in text and re.search(r": ok, \d+ facts", text), text
                # a call is one line, not the three events it was made of
                assert not any(re.search(r"\bchose call_tool\b|\bran \w+ on the machine\b", x[1]) for x in texts)
                # the start and the end of the run are there, whatever the rate limit says
                assert any("analysis run started" in x[1] for x in texts) and any("the analysis run ended: done" in x[1] for x in texts)
                assert any(a == "OWNER" and "run started" in x for a, x in texts)  # the owner asked for it: the line says who did
            finally:
                await down(agent, task)
    run(go())


# ---- the rate limit -------------------------------------------------------------------------------------------------------------------------------------

def test_ordinary_lines_wait_for_the_gap_but_an_important_line_or_the_end_of_a_run_releases_them_at_once(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            core, now = c.harness, [100.0]
            core.clock = lambda: now[0]
            rid = "r1"
            r = fake_run(core)
            core._logs[rid] = []
            core._buf[rid] = lines_of(core, [("MODEL", "a ok"), ("MODEL", "b ok")])
            core._flush(r, "internal")  # first flush of this run: nothing came before, so the gap is already over
            assert [i.text_fallback for i in core._feed] == ["Lenovo · a ok", "Lenovo · b ok"]
            core._buf[rid] = lines_of(core, [("MODEL", "c ok")])
            now[0] += 0.5
            core._flush(r, "internal")
            assert len(core._feed) == 2 and core._buf[rid]  # held: 0.5 s is inside the 2 s gap
            now[0] += 2.0
            core._flush(r, "internal")
            assert [i.text_fallback for i in core._feed][-1] == "Lenovo · c ok" and not core._buf[rid]
            core._buf[rid] = lines_of(core, [("MODEL", "d ok"), ("OWNER", "you answered q: x")], important=("you answered q: x",))
            now[0] += 0.1
            core._flush(r, "internal")  # an important line: shown now, with the ordinary one that waited, in order
            assert [i.text_fallback for i in core._feed][-2:] == ["Lenovo · d ok", "Lenovo · you answered q: x"]
            core._buf[rid] = lines_of(core, [("GUARD", "e ok")])
            now[0] += 0.1
            r.finished = now[0]  # the end of the run: nothing is left behind
            core._flush(r, "internal")
            assert core._feed[-1].text_fallback == "Lenovo · e ok" and not core._buf[rid]
    run(go())


def test_a_burst_comes_out_as_a_few_lines_with_the_rest_merged_into_one_that_says_who_did_each(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            core = c.harness
            r = fake_run(core)
            core._logs["r1"] = []
            burst = [("MODEL" if n % 2 else "SCRIPT", f"probe{n} ok") for n in range(30)] + [("OWNER", "you picked option o1")]
            core._buf["r1"] = lines_of(core, burst, important=("you picked option o1",))
            core._flush(r, "internal")
            shown = [i.text_fallback for i in core._feed]
            assert len(shown) <= FEED_MAX_LINES and shown[-1] == "Lenovo · you picked option o1"  # an important line is never merged away
            merged = next(x for x in shown if "more steps" in x)
            assert merged.startswith("Lenovo · ") and "MODEL probe1 ok" in merged and "SCRIPT probe0 ok" in merged  # the machine, the count and who did each, in the one line
            assert re.search(r"· (\d+) more steps", merged) and int(re.search(r"· (\d+) more steps", merged).group(1)) >= 25
            assert core._feed[-1].actor == "OWNER" and len({i.actor for i in core._feed if "more steps" in i.text_fallback}) == 1
    run(go())


# ---- tasks nobody's run owns ----------------------------------------------------------------------------------------------------------------------

def test_a_task_the_owner_asked_for_on_a_machine_shows_up_with_the_machine_its_type_and_who_asked(tmp_path):
    async def go():
        async with coordinator(tmp_path, web=True) as c:
            agent, task, nid = await node_up(c, tmp_path)
            try:
                rec = c.run_task(nid, "os_details")
                await settle(lambda: c.tasks.tasks and all(t.status in ("done", "failed", "rejected", "timed_out") for t in c.tasks.tasks.values()))
                out = c.harness.activity(CLAUDE, 0)
                lines = [(i["actor"], i["text_fallback"]) for i in out["intents"]]
                assert ("OWNER", "node1 · task os_details: done") in lines, lines
                assert out["seq"] > 0 and c.harness.activity(CLAUDE, out["seq"])["intents"] == []  # nothing is told twice
            finally:
                await down(agent, task)
    run(go())


# ---- sensitivity -----------------------------------------------------------------------------------------------------------------------------------

def test_the_activity_of_a_confidential_run_is_withheld_from_a_cloud_channel(tmp_path):
    async def go():
        async with coordinator(tmp_path, web=True) as c:
            agent, task, nid = await node_up(c, tmp_path)
            scripted(c)
            c.harness.feed_gap_s = 0
            try:
                view = c.harness.start(OWUI, "analyze", nid, "confidential")  # a local chat may create one
                r = c.runs.runs[view["id"]]
                got_cloud, _ = await follow(c.harness, r, CLAUDE)
                cloud = [i["text_fallback"] for i in got_cloud if i["run_id"] == r.id]
                local = [i["text_fallback"] for i in c.harness.activity(OWUI, 0)["intents"] if i["run_id"] == r.id]
                assert cloud and all("Withheld" in x for x in cloud) and not any("node1" in x for x in cloud)
                assert any("node1" in x and "chosen by" in x for x in local)
            finally:
                await down(agent, task)
    run(go())


# ---- the three hosts ------------------------------------------------------------------------------------------------------------------------------------

def test_claude_desktop_follows_the_machines_with_one_tool_and_is_told_each_line_once(tmp_path):
    async def go():
        async with coordinator(tmp_path, web=True) as c:
            agent, task, nid = await node_up(c, tmp_path)
            scripted(c)
            c.harness.feed_gap_s = 0
            host = FakeHost(c, tmp_path, caps={"extensions": {"io.modelcontextprotocol/ui": {"mimeTypes": ["text/html;profile=mcp-app"]}}})
            try:
                await host.start()
                tools = (await host.rpc("tools/list"))["result"]["tools"]
                assert "mas_activity" in [t["name"] for t in tools if "visibility" not in t["_meta"].get("ui", {})]
                start = await host.tool("mas_start", goal="analyze", node="node1")
                run_id = start["structuredContent"]["run_id"]
                chunks, lines_total = [], 0
                while True:
                    done = c.runs.runs[run_id].task.done()
                    got = await host.tool("mas_activity")
                    if got["structuredContent"]["lines"]:
                        chunks.append(got["content"][0]["text"])
                        lines_total += got["structuredContent"]["lines"]
                    if done:
                        break
                first = {"content": [{"type": "text", "text": "\n".join(chunks)}], "structuredContent": {"lines": lines_total}}
                text = first["content"][0]["text"]
                assert re.search(r"^\[MODEL\] node1 · \w+: ok, \d+ facts \(chosen by MODEL, run by GUARD\)$", text, re.M) and "[GUARD] node1 · the analysis run ended: done" in text
                assert first["structuredContent"]["lines"] == len(text.splitlines()) and "<svg" not in text
                again = await host.tool("mas_activity")
                assert again["content"][0]["text"] == "Nothing new on the machines." and again["structuredContent"]["lines"] == 0
            finally:
                await host.stop()
                await down(agent, task)
    run(go())


def test_open_webui_shows_what_the_machines_do_in_its_status_line_including_what_the_run_does_not_own(tmp_path):
    async def go():
        async with coordinator(tmp_path, web=True) as c:
            agent, task, nid = await node_up(c, tmp_path)
            scripted(c)
            c.harness.feed_gap_s = 0

            async def panel_task():
                await asyncio.sleep(0.4)
                c.run_task(nid, "os_details")  # the owner asks the node for something else from the panel, meanwhile
            side = asyncio.create_task(panel_task())
            try:
                chat = Chat(["2"])
                await load_pipe(c.state.parent).pipe({"messages": []}, {}, chat.emit, chat.call)
                await side
                st = [e["data"]["description"] for e in chat.events if e["type"] == "status"]
                assert any(re.match(r"^(MODEL|GUARD): node1 · ", x) for x in st), st  # the run's own lines, with machine and actor
                assert st[-1] == "Done"
                # not more than one line per fact of what happened: nothing is repeated
                assert len(st) == len(set(st)) or len(st) - len(set(st)) <= 2
            finally:
                await down(agent, task)
    run(go())


def test_the_panel_gets_the_same_stream_as_a_fallback_and_draws_it_without_innerhtml(tmp_path):
    async def go():
        async with coordinator(tmp_path, web=True) as c:
            agent, task, nid = await node_up(c, tmp_path)
            scripted(c)
            c.harness.feed_gap_s = 0
            try:
                r = c.runs.start_analysis(nid, "web", model=ScriptedModel(list(ANALYSIS_SCRIPT)), status=AVAILABLE)
                await follow(c.harness, r, CLAUDE)
                snap = c.snapshot()
                assert snap["activity"] and all(set(x) >= {"actor", "text", "ts"} for x in snap["activity"])
                assert any("chosen by MODEL" in x["text"] and x["text"].startswith("node1 · ") for x in snap["activity"])
            finally:
                await down(agent, task)
        js = (ROOT / "src/envrec_coordinator/web_static/app.js").read_text()
        body = js[js.index("function renderActivity"): js.index("function accept")]
        assert "innerHTML" not in body and "textContent" in body and "createTextNode" in body  # every word of a line goes in as text
        assert 'id="activity"' in (ROOT / "src/envrec_coordinator/web_static/index.html").read_text()
    run(go())

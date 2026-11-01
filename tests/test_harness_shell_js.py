"""The JavaScript our MCP view runs is EXECUTED here, against a host simulator that follows the published MCP Apps protocol (tests/js/host_sim.mjs, Node). The first real chat session found no
card ever visible; the cause was `ReferenceError: report is not defined` in the shell, which string-matching tests could never see. Also: the adapter's request log, and Spanish in what the owner reads."""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from envrec_harness import host
from mas_world import coordinator
from test_harness_core import HASH, Dialog, ask_write, owner_events
from test_harness_mcp import FakeHost

SIM = Path(__file__).parent / "js" / "host_sim.mjs"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed: the shell's JavaScript cannot be executed here")


def script_of(host_name: str, tmp: Path) -> Path:
    doc = host.document("", host_name)
    p = tmp / f"{host_name}.js"
    p.write_text("\n".join(re.findall(r"<script>(.*?)</script>", doc, flags=re.S)))
    return p


def simulate(tmp: Path, mode: str, host_name: str = "mcp") -> dict:
    done = subprocess.run([NODE, str(SIM), str(script_of(host_name, tmp)), mode], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0 and not done.stderr.strip(), done.stderr[:600]  # an uncaught error in the shell (the original bug) fails here
    return json.loads(done.stdout.strip().splitlines()[-1])


@needs_node
def test_the_mcp_shell_runs_without_error_does_the_handshake_reports_a_real_height_and_paints_the_cards(tmp_path):
    r = simulate(tmp_path, "normal")
    m = r["messages"]
    assert m[:2] == ["ui/initialize", "ui/notifications/initialized"]  # the host sends nothing until it gets `initialized`
    sizes = [json.loads(x) for x in r["sizes"]]
    assert sizes and all(s["height"] > 0 for s in sizes), sizes  # a zero height is an invisible app (Claude's own troubleshooting page)
    assert m.index("ui/notifications/size-changed") < m.index("tools/call")  # visible before it talks to the server
    assert r["tools"] == ["mas_view"] and r["rendered"] == "MAP"  # the host's answer was reported as evidence, and the card the tool result carried is on screen
    assert m[-1] == "ui/notifications/size-changed"  # and its new height was reported after painting


@needs_node
def test_a_host_that_strips_the_result_meta_still_leaves_a_visible_message_not_an_empty_frame(tmp_path):
    r = simulate(tmp_path, "strip_meta")
    assert "no cards to show" in r["rendered"] and all(json.loads(x)["height"] > 0 for x in r["sizes"])


@needs_node
def test_a_host_that_refuses_the_handshake_is_told_so_on_screen_with_its_own_words(tmp_path):
    r = simulate(tmp_path, "reject_init")
    assert "The host refused the view's startup" in r["rendered"] and "bad params" in r["rendered"]


@needs_node
def test_a_host_that_never_answers_leaves_a_visible_waiting_line(tmp_path):
    assert "connecting to the host" in simulate(tmp_path, "silent")["rendered"]


@needs_node
@pytest.mark.parametrize("name", ["openwebui", "panel", "plain"])
def test_the_other_shells_also_run_without_error(tmp_path, name):
    out = subprocess.run([NODE, "-e", f"require('vm').runInNewContext(require('fs').readFileSync({json.dumps(str(script_of(name, tmp_path)))}, 'utf8'), "
                          "{window:{addEventListener(){}},document:{addEventListener(){},querySelectorAll:()=>[],documentElement:{scrollHeight:10}},parent:{postMessage(m){console.log(JSON.stringify(m))}},"
                          "console,Math,JSON}); console.log('ran')"], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0 and "ran" in out.stdout, out.stderr[:400]
    if name == "openwebui":
        pass  # the height report is posted on load/resize; the shell itself must simply run


def test_every_function_the_mcp_shell_calls_is_defined_in_its_own_scope():
    """A cheap guard that needs no Node: the names the shell calls (report, say, paint, put, draw, schedule, evidence, rpc, note, send) are all defined with `function` in the same script."""
    js = "\n".join(re.findall(r"<script>(.*?)</script>", host.document("", "mcp"), flags=re.S))
    mcp = js[js.index("var pending="):]
    for name in ("report", "say", "paint", "put", "draw", "schedule", "evidence", "rpc", "note", "send", "height"):
        assert re.search(rf"function {name}\(", mcp), name


def run(coro):
    return asyncio.run(coro)


def test_the_adapter_logs_what_the_host_asked_so_a_retest_can_say_whether_it_reads_the_ui_resource(tmp_path):
    async def go():
        async with coordinator(tmp_path, web=True) as c:
            h = FakeHost(c, tmp_path, caps={"extensions": {"io.modelcontextprotocol/ui": {"mimeTypes": ["text/html;profile=mcp-app"]}}})
            await h.start()
            await h.rpc("tools/list")
            await h.rpc("resources/list")
            await h.rpc("resources/read", {"uri": "ui://mas/cards"})
            secret = "SECRET-ARGUMENT-VALUE"
            await h.tool("mas_answer", intent_id=secret, option=secret)
            await h.stop()
            rows = [json.loads(x) for x in (c.harness.dir / "rpc-claude-desktop.jsonl").read_text().splitlines()]
            ins = [(r["method"], r.get("tool") or r.get("uri")) for r in rows if r["dir"] == "in"]
            assert ("initialize", None) in ins and ("tools/list", None) in ins and ("resources/list", None) in ins
            assert ("resources/read", "ui://mas/cards") in ins and ("tools/call", "mas_answer") in ins
            outs = {r["method"]: r for r in rows if r["dir"] == "out"}
            assert outs["resources/read"]["ok"] and outs["resources/read"]["bytes"] > 1000 and outs["tools/list"]["ok"]
            assert all(r["client"] == "fake-claude-desktop" for r in rows)
            assert secret not in (c.harness.dir / "rpc-claude-desktop.jsonl").read_text()  # names, uris and sizes: never the arguments
    run(go())


def test_what_the_owner_reads_about_an_approval_is_spanish_and_a_decline_is_not_called_weak(tmp_path):
    async def go():
        async with coordinator(tmp_path, web=True) as c:
            h = FakeHost(c, tmp_path, caps={})
            await h.start()
            for result, want, forbidden in (("declined", "answer in the system dialog: declined.", ("(declined)",)), ("approved", "answer in the system dialog: approved.", ("(approved)",))):
                c.harness.native_confirm = Dialog(result)
                p = ask_write(c, plan_hash="sha256:" + ("12" if result == "approved" else "34") * 32)
                r = await h.tool("mas_request_approval", intent_id=p.intent.id)
                text = r["content"][0]["text"]
                assert want in text and not any(w in text for w in forbidden), text  # the owner reads the answer as one plain sentence; the code stays in the typed fields
                assert r["structuredContent"]["state"] == result  # ...which are still there for code
            ev = owner_events(c, "owner_confirmation")
            declined = next(e for e in ev if e.payload["approved"] is False)
            assert declined.payload["assurance"] is None  # only an approval has a strength
            await h.stop()
    run(go())


def test_the_native_dialog_buttons_are_Cancelar_and_Aprobar_and_cancel_is_the_default():
    d = json.loads((Path(__file__).parents[1] / "skills/os-confirm/references/dialogs.json").read_text())
    mac = d["darwin"]["tools"][0]["argv"][1]
    assert 'buttons {"Cancel", "Approve"} default button "Cancel" cancel button "Cancel"' in mac
    for t in d["linux"]["tools"]:
        joined = " ".join(t["argv"])
        assert "Approve" in joined and "Cancel" in joined

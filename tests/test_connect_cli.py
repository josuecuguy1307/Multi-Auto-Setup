"""`mas connect` and `mas disconnect` at the owner's terminal, and the panel's entry. SIMULATED-LOCAL: a real coordinator with its owner screen, a scripted model, a temporary home; the terminal is played by a function
that answers what the owner would type. Nothing here touches a real settings file."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from envrec_coordinator.harness import CHANNELS
from envrec_harness.client import CoreClient
from envrec_mas import cli
from mas_world import coordinator
from test_connect import SERVER, home, make_env, other_servers_file  # noqa: F401
from test_harness_connect import CONNECT, DISCONNECT, script
from test_mas_agent import act

ROOT = Path(__file__).resolve().parents[1]


def run(coro):
    return asyncio.run(coro)


class Terminal:
    def __init__(self, answers):
        self.answers, self.said, self.asked = list(answers), [], []

    def read(self, prompt):
        self.asked.append(prompt)
        return self.answers.pop(0)

    def write(self, text):
        self.said.append(text)


async def session(c, goal, host, term):
    core = CoreClient(c.harness.dir, "cli", timeout=60.0)
    await asyncio.to_thread(core.declare, {"event_call": True, "host": "terminal"})  # a blocking call off the loop the coordinator runs on
    return await asyncio.to_thread(cli.connect_session, core, goal, host, read=term.read, write=term.write, wait=1.0)


def test_the_owner_connects_and_disconnects_claude_desktop_at_their_terminal_typing_yes_for_each_step(tmp_path, home):
    async def go():
        cfg = home / "Library/Application Support/Claude/claude_desktop_config.json"
        original = other_servers_file(cfg)
        async with coordinator(tmp_path / "c", web=True) as c:
            c.connect_env = make_env(tmp_path, home)
            script(c)
            term = Terminal(["yes"])  # approval-policy.json: a connection is ONE question for the whole plan, and his typed yes at his terminal answers it
            assert await session(c, "connect_host", "claude-desktop", term) == 0
            said = "\n".join(term.said)
            assert "Plan to connect Claude Desktop to Multi Auto Setup" in said and "Step 1 of 3" in said and "The run ended: done." in said
            assert said.index("Plan to connect") < said.index("[GUARD]") or "Plan to connect" in said  # the plan is printed before any step is asked
            assert len(term.asked) == 1 and all("Type yes" in q and "plan" in q for q in term.asked)
            assert json.loads(cfg.read_text())["mcpServers"][SERVER]["args"][-1] == "claude-desktop"
            ev = [e.payload for e in c.log.events("owner_confirmation")]
            assert len(ev) == 1 and all(p["channel"] == "cli" and p["mechanism"] == "chat_choice" and p["approved"] for p in ev)  # recorded as the one plan question it was
            assert "mas_request_approval" not in said and "call mas_request" not in "\n".join(term.asked) and said.count("Type yes below to approve this plan") == 1  # the terminal shows its own instruction, never the chat's
            term = Terminal(["yes", "yes", "yes"])
            assert await session(c, "disconnect_host", "claude-desktop", term) == 0
            assert cfg.read_text() == original and "Plan to undo" in "\n".join(term.said)
    run(go())


def test_anything_but_yes_declines_the_step_and_nothing_is_written(tmp_path, home):
    async def go():
        cfg = home / "Library/Application Support/Claude/claude_desktop_config.json"
        original = other_servers_file(cfg)
        async with coordinator(tmp_path / "c", web=True) as c:
            c.connect_env = make_env(tmp_path, home)
            script(c, connect=[act("detect_hosts"), act("ask_hosts"), act("build_connect_plan"), act("run_step", args={"step": "1"}), act("finish_connect", args={"summary": "stopped"})])
            term = Terminal(["no"])
            code = await session(c, "connect_host", "claude-desktop", term)
            assert code == 1 and cfg.read_text() == original and not list(cfg.parent.glob("*.mas-backup-*"))
            assert [e.payload["approved"] for e in c.log.events("owner_confirmation")] == [False]
    run(go())


def test_a_terminal_without_a_person_is_refused_and_the_cli_channel_has_no_model_in_the_middle(monkeypatch, capsys):
    class Args:
        cmd, host, state_dir = "connect", "codex", None
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": lambda self: False})())
    assert cli.cmd_connect(Args()) == cli.EXIT_USAGE and "needs you at the terminal" in capsys.readouterr().err
    ch = CHANNELS["cli"]
    assert ch.model_in_middle is False and ch.cloud is False and ch.mechanisms == ("cli_typed", "os_dialog")


def test_the_panel_starts_a_connection_run_and_the_page_has_its_button(tmp_path, home):
    async def go():
        async with coordinator(tmp_path / "c", web=True) as c:
            c.connect_env = make_env(tmp_path, home)
            script(c)
            view = c.start_connect_run("codex", "web")
            assert view["kind"] == "connect"
            c.runs.runs[view["id"]].task.cancel()
        assert 'id="connect-chat"' in (ROOT / "src/envrec_coordinator/web_static/index.html").read_text()
        js = (ROOT / "src/envrec_coordinator/web_static/app.js").read_text()
        assert '"/api/connect/start"' in js and 'run.kind === "connect"' in js and "innerHTML" not in js[js.index("async function connectChat"): js.index("async function askLanAgent")]
        strings = json.loads((ROOT / "src/envrec_coordinator/web_static/strings.en.json").read_text())
        assert strings["chats.connect"] == "Connect a chat" and strings["run.title.connect"]
    run(go())


def test_the_openwebui_account_is_shown_on_the_owners_screen_and_at_their_terminal_and_never_to_a_chat_door(tmp_path, home, capsys, monkeypatch):
    from test_mas_web import Browser

    async def go():
        d = home / "mas-openwebui"
        d.mkdir()
        (d / "admin.secret").write_text(json.dumps({"email": "admin@localhost", "password": "pw-for-the-test-1234567890"}))
        async with coordinator(tmp_path / "c", web=True) as c:
            c.connect_env = make_env(tmp_path, home)
            b = Browser(c)
            status, _, _ = await b.raw("GET", "/api/openwebui/admin", cookie=False)
            assert status == 401  # no session: nothing
            await b.open()
            state = await b.state()
            assert state["openwebui_admin"] is True and "pw-for-the-test" not in json.dumps(state)  # the snapshot says only that an account exists
            status, _, data = await b.raw("GET", "/api/openwebui/admin")
            assert status == 200 and json.loads(data) == {"email": "admin@localhost", "password": "pw-for-the-test-1234567890"}
            for ch in ("claude-desktop", "openwebui", "terminal", "cli"):
                status, _, _ = await b.raw("GET", "/api/openwebui/admin", headers={"Authorization": f"Bearer {c.harness.tokens[ch]}"}, cookie=False)
                assert status == 401  # a chat's token opens only /harness/v1
            assert "admin" not in json.dumps([i for i in c.harness.activity(CHANNELS["claude-desktop"], 0)["intents"]])
    run(go())
    monkeypatch.setenv("HOME", str(home))

    class Args:
        state_dir = str(tmp_path / "state")
    from envrec_coordinator import connect as C
    monkeypatch.setattr(C.Env, "system", classmethod(lambda cls, sd: make_env(tmp_path, home)))
    assert cli.cmd_openwebui_account(Args()) == 0
    out = capsys.readouterr().out
    assert "admin@localhost" in out and "pw-for-the-test-1234567890" in out
    js = (ROOT / "src/envrec_coordinator/web_static/app.js").read_text()
    body = js[js.index("async function showOpenWebuiAccount"): js.index("async function connectChat")]
    assert "innerHTML" not in body and "textContent" in body


def test_the_end_of_a_connect_run_never_prints_the_generated_admin_secret_and_points_to_the_command_that_does(tmp_path, home, capsys, monkeypatch):
    import sys
    import envrec_harness.client as client
    d = home / "mas-openwebui"
    d.mkdir()
    (d / "admin.secret").write_text(json.dumps({"email": "admin@localhost", "password": "pw-for-the-test-1234567890"}))
    monkeypatch.setenv("HOME", str(home))

    class Core:
        def __init__(self, *a, **k): ...
        def declare(self, caps): ...

    class Tty:
        def isatty(self): return True
    monkeypatch.setattr(client, "CoreClient", Core)
    monkeypatch.setattr(cli, "connect_session", lambda *a, **k: 0)
    monkeypatch.setattr(sys, "stdin", Tty())

    class Args:
        cmd, host, state_dir = "connect", "openwebui", str(tmp_path / "state")
    assert cli.cmd_connect(Args()) == 0
    out = capsys.readouterr().out
    assert "pw-for-the-test" not in out and "admin@localhost" not in out and "mas openwebui-account" in out


def test_a_plan_that_moves_a_folder_says_moved_and_never_says_downloaded_while_a_plan_that_installs_still_says_download():
    from envrec_harness import cards
    step = {"n": 1, "title": "Move it", "exact_command": "Move the folder", "writes": ["~/a/"], "undo": "Move it back", "needs_owner_approval": True}
    moves = {"connect": True, "plan_title": "Remove", "plan_hash": "sha256:" + "a" * 64, "machine": "this computer", "option_title": "x", "total_download_bytes": 0, "total_moved_bytes": 1_800_000_000,
             "steps": [{**step, "bytes": 1_800_000_000, "size_kind": "move"}], "verification": {"what": "gone"}}
    text = cards.fallback_text("plan", moves, "en")
    assert "moved, not downloaded" in text and "nothing is downloaded" in text and "downloaded in total" not in text and "to download" not in text
    installs = {**moves, "total_download_bytes": 1_800_000_000, "total_moved_bytes": 0, "steps": [{**step, "bytes": 1_800_000_000, "size_kind": "download"}]}
    text = cards.fallback_text("plan", installs, "en")
    assert "downloaded in total" in text and "moved" not in text

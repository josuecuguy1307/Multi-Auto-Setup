"""The Claude Desktop entry survives updates (P2, finding F): an entry whose command is gone is not "connected" and is offered for repair; `mas update` rewrites such an entry to the stable command and tells
the owner to quit and reopen the chat; a chat that is running is not edited under; `mas connect` never answers "nothing to connect" for a dead entry. SIMULATED-LOCAL: a temporary home, fake commands."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from test_connect import SERVER, home, make_env, other_servers_file  # noqa: F401
from test_update import World, args, installed  # noqa: F401

from envrec_coordinator import connect as C
from envrec_mas import lifecycle

CFG = "Library/Application Support/Claude/claude_desktop_config.json"


def stable_env(tmp_path, home, **kw):
    """An environment whose launcher is the installer's stable command (a file with the installer's mark)."""
    shim = tmp_path / "shim" / "mas"
    shim.parent.mkdir(exist_ok=True)
    shim.write_text("#!/bin/sh\n# written by the Multi Auto Setup installer\n")
    shim.chmod(0o755)
    env = make_env(tmp_path, home, **kw)
    env.launcher = (str(shim),)
    return env


def write_entry(home, command, extra=None):
    cfg = home / CFG
    secret = other_servers_file(cfg)
    doc = json.loads(secret)
    doc["mcpServers"][SERVER] = {"command": command, "args": ["-m", "envrec_mas", "--state-dir", "/x", "mcp", "--channel", "claude-desktop"], **(extra or {})}
    cfg.write_text(json.dumps(doc, indent=2) + "\n")
    return cfg, doc["mcpServers"]["other"]


def test_an_entry_whose_command_does_not_exist_is_not_connected_and_is_offered_for_repair(tmp_path, home):
    write_entry(home, str(tmp_path / "releases" / "gone" / ".venv" / "bin" / "python"))
    row = {r["host"]: r for r in C.detect(stable_env(tmp_path, home))}["claude-desktop"]
    assert row["state"] == "repair" and row["code"] == "dead" and "does not exist" in row["detail"] and "connected" not in row["state"]


def test_an_entry_inside_a_release_folder_is_offered_for_repair_while_the_stable_command_exists(tmp_path, home, monkeypatch):
    mas_home = tmp_path / "mashome"
    py = mas_home / "releases" / "v1" / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("")
    monkeypatch.setenv("MAS_HOME", str(mas_home))
    write_entry(home, str(py))
    row = {r["host"]: r for r in C.detect(stable_env(tmp_path, home))}["claude-desktop"]
    assert row["state"] == "repair" and row["code"] == "release" and "mas update" in row["detail"]
    assert {r["host"]: r for r in C.detect(make_env(tmp_path, home))}["claude-desktop"]["state"] == "connected"  # from a source checkout (no stable command) a working command is left alone


def test_a_good_entry_is_connected_and_a_repaired_one_becomes_connected(tmp_path, home):
    env = stable_env(tmp_path, home)
    write_entry(home, str(tmp_path / "nowhere" / "python"))
    plan = C.build_plan("claude-desktop", env)  # a dead entry of ours is replaced by a plan, not refused as already connected
    for st in plan["steps"]:
        C.run_step(plan, st["n"], "run", env)
    assert {r["host"]: r for r in C.detect(env)}["claude-desktop"]["state"] == "connected"
    assert json.loads((home / CFG).read_text())["mcpServers"][SERVER]["command"] == env.launcher[0]


def test_repair_rewrites_only_our_entry_keeps_a_copy_and_says_to_quit_and_reopen(tmp_path, home):
    cfg, other = write_entry(home, str(tmp_path / "nowhere" / "python"))
    env = stable_env(tmp_path, home)
    rows = C.repair_entries(env)
    assert [(r["host"], r["state"]) for r in rows] == [("claude-desktop", "repaired")] and "Cmd+Q" in rows[0]["text"] and "open it again" in rows[0]["text"]
    doc = json.loads(cfg.read_text())
    assert doc["mcpServers"][SERVER]["command"] == env.launcher[0] and doc["mcpServers"]["other"] == other and doc["theme"] == "dark"  # the other entry and its secret are untouched
    assert list(cfg.parent.glob("*.mas-backup-*"))
    assert C.repair_entries(env) == []  # nothing left to repair


def test_a_chat_that_is_running_is_not_edited_under_and_the_owner_is_told_what_to_do(tmp_path, home):
    cfg, _ = write_entry(home, str(tmp_path / "nowhere" / "python"))
    before = cfg.read_text()
    rows = C.repair_entries(stable_env(tmp_path, home, running=True))
    assert rows[0]["state"] == "needs_quit" and rows[0]["code"] == "app_running" and "Cmd+Q" in rows[0]["text"] and "mas connect claude-desktop" in rows[0]["text"] and "ask for the connection again" in rows[0]["text"]
    assert cfg.read_text() == before and not list(cfg.parent.glob("*.mas-backup-*"))


def test_mas_update_shows_the_repair_beforehand_and_does_it_after_the_switch(installed, tmp_path, home, monkeypatch, capsys):
    cfg, other = write_entry(home, str(tmp_path / "nowhere" / "python"))
    env = stable_env(tmp_path, home)
    monkeypatch.setattr(C.Env, "system", classmethod(lambda cls, state_dir: env))
    w = World(tmp_path)
    assert lifecycle.cmd_update(args(), fetch=w.fetch, run=w.run, smoke_via_pointer=lambda lay: lay.current == "v2") == 0
    out = capsys.readouterr().out
    assert "entry of Claude Desktop" in out and "is rewritten to run the stable command" in out and "Cmd+Q" in out and "open it again" in out
    assert json.loads(cfg.read_text())["mcpServers"][SERVER]["command"] == env.launcher[0] and json.loads(cfg.read_text())["mcpServers"]["other"] == other


def test_mas_update_with_a_running_chat_leaves_its_file_and_says_what_to_do(installed, tmp_path, home, monkeypatch, capsys):
    cfg, _ = write_entry(home, str(tmp_path / "nowhere" / "python"))
    before = cfg.read_text()
    env = stable_env(tmp_path, home, running=True)
    monkeypatch.setattr(C.Env, "system", classmethod(lambda cls, state_dir: env))
    w = World(tmp_path)
    assert lifecycle.cmd_update(args(), fetch=w.fetch, run=w.run, smoke_via_pointer=lambda lay: lay.current == "v2") == 0
    cap = capsys.readouterr()
    assert cfg.read_text() == before and "Cmd+Q" in cap.err and "mas connect claude-desktop" in cap.err and "Updated to v2" in cap.out


def test_a_chat_check_that_fails_never_stops_an_update(installed, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(C.Env, "system", classmethod(lambda cls, state_dir: (_ for _ in ()).throw(RuntimeError("boom"))))
    w = World(tmp_path)
    assert lifecycle.cmd_update(args(), fetch=w.fetch, run=w.run, smoke_via_pointer=lambda lay: lay.current == "v2") == 0


def test_mas_connect_makes_a_plan_for_a_dead_entry_instead_of_nothing_to_connect(tmp_path, home):
    import asyncio

    from mas_world import coordinator
    from test_harness_connect import CLAUDE, script
    from test_harness_setup import until
    from test_mas_agent import act

    async def go():
        write_entry(home, str(tmp_path / "nowhere" / "python"))
        async with coordinator(tmp_path / "c", web=True) as c:
            c.connect_env = stable_env(tmp_path, home)
            script(c, connect=[act("detect_hosts"), act("build_connect_plan")])
            r = c.runs.runs[c.harness.start(CLAUDE, "connect_host", "claude-desktop")["id"]]
            await until(lambda: any(f.key == "plan.built" for f in r.store.current_facts(r.target.id)), 20)
            facts = {f.key: f.value for f in r.store.current_facts(r.target.id)}
            assert facts["plan.built"] is True and "setup.refused" not in facts and r.status != "nothing_to_connect"
            r.task.cancel()
    asyncio.run(go())

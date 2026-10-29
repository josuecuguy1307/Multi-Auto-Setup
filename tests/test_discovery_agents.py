"""D3: other agents and MCP servers. Privacy: no argument, environment value or token ever reaches a fact or the raw store."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from envrec.contracts import Target, TargetKind
from envrec.discovery import Capture, Cmd, LocalCollector, Reason, ReplayCollector, Salts
from envrec.privacy.reducers import reduce_mcp_servers as reduce_mcp
from envrec.skills.discover import get_adapter, run_adapter
from skillfactory import load_agents
from envrec.store import Store

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "discovery" / "app_agents"
SECRET = "sk-TOP-SECRET-TOKEN-123"
RAW_CONFIG = json.dumps({"mcpServers": {
    "files": {"command": "/opt/tools/bin/npx", "args": ["-y", "server-files", "--root", "/Users/someone/private", "--token", SECRET],
              "env": {"API_KEY": SECRET, "HOME_DIR": "/Users/someone"}},
    "remote": {"url": "https://example.test/mcp", "headers": {"Authorization": "Bearer " + SECRET}},
}, "unrelated": {"password": SECRET}}).encode()


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "s") as s:
        yield s


def host(store):
    return store.put_target(Target(kind=TargetKind.host, ring=0, handle="h"))


def agents(store, plat):
    h = host(store)
    r = run_adapter(get_adapter("app_agents"), ReplayCollector(FIX / plat, plat), store, h, salts=Salts(), platform=plat)
    ents = {f.value["attrs"]["name"]: f.value["attrs"] for k, f in ((f.key, f) for f in store.current_facts(h.id)) if ".entity." in k}
    return r, ents


def test_table_has_sources_and_covers_the_categories():
    spec = load_agents()
    cats = {a["category"] for a in spec["agents"]}
    assert {"coding_agent", "ide_assistant", "local_model_runner", "agent_framework", "assistant_app"} <= cats
    assert all(a["src"].startswith("https://") and (a.get("bins") or a.get("apps") or a.get("config_dirs") or a.get("processes")) for a in spec["agents"])


def test_installed_and_running_and_absent_from_synthetic_linux(store):
    r, ents = agents(store, "linux")
    assert r.status == "ran"
    assert ents["Claude Code"]["state"] == "running" and set(ents["Claude Code"]["seen_as"]) >= {"process", "binary", "config"}
    assert ents["Ollama"]["state"] == "running"
    assert "Cursor" not in ents and "Aider" not in ents  # absent agents are not invented


def test_a_user_program_listening_on_a_port_with_an_agent_like_name_becomes_an_unknown(store):
    r, ents = agents(store, "linux")
    assert "my-agent-gateway" in ents and r.unknowns >= 1
    h = store.list_targets()[0]
    ent = next(f for f in store.current_facts(h.id) if ".entity." in f.key and f.value["attrs"].get("name") == "my-agent-gateway")
    assert ent.value["resolved"] is False and ent.value["kind"] == "unknown_agent"


def test_mcp_servers_are_names_and_command_basenames_only_from_synthetic(store):
    _, ents = agents(store, "linux")
    assert ents["files"]["command"] == "npx" and ents["search"]["command"] == "uvx"
    assert set(ents["files"]) == {"name", "command", "client"}


def test_real_mac_fixture_finds_agents_that_are_present(store):
    r, ents = agents(store, "macos")
    assert r.status == "ran" and any(a["category"] in ("coding_agent", "local_model_runner", "assistant_app") for a in ents.values())


def test_reduce_mcp_drops_everything_but_name_and_command_basename():
    red = json.loads(reduce_mcp(RAW_CONFIG))
    assert red == {"files": "npx", "remote": "(remote url)"}
    assert SECRET.encode() not in reduce_mcp(RAW_CONFIG) and b"someone" not in reduce_mcp(RAW_CONFIG)
    assert reduce_mcp(b"not json") == b"{}"
    assert json.loads(reduce_mcp(reduce_mcp(RAW_CONFIG))) == red  # idempotent on an already reduced capture


class FakeHome(LocalCollector):
    """A local-looking collector whose MCP config file holds real-looking secrets."""

    def run(self, cmd: Cmd):
        return Capture(cmd.name, " ".join(cmd.argv), b"", 1, "local", "macos", Reason.unavailable, "x")

    def read_file(self, name, path):
        return Capture(name, "file:" + path, RAW_CONFIG, 0, "local", "macos") if name == "mcp_0" else Capture(name, "file:" + path, b"", None, "local", "macos", Reason.not_applicable)


def test_no_secret_reaches_any_fact_or_the_raw_store(store, tmp_path):
    ps = Capture("ps_comm", "ps", b"/usr/bin/claude\n", 0, "local", "macos")

    class C(FakeHome):
        def run(self, cmd):
            return ps if cmd.name == "ps_comm" else super().run(cmd)
    h = host(store)
    r = run_adapter(get_adapter("app_agents"), C("macos"), store, h, salts=Salts(), platform="macos")
    assert r.status == "ran"
    everything = " ".join(json.dumps(f.model_dump(mode="json")) for f in store.current_facts(h.id))
    assert "files" in everything and "npx" in everything
    for needle in (SECRET, "someone", "Authorization", "API_KEY", "private"):
        assert needle not in everything
    raw = b"".join(p.read_bytes() for p in (store.root / "raw").rglob("*") if p.is_file())
    for needle in (SECRET, "someone", "Authorization", "API_KEY", "/Users/someone/private"):
        assert needle.encode() not in raw

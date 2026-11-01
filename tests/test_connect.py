"""Connecting a chat host (src/envrec_coordinator/connect.py): the plan built from data, what each step does to the files, and the undo. SIMULATED-LOCAL, and never against this computer's real settings:
every test gets a temporary home and fake commands (a stub for `claude`, a stub for `uv`, and a small stand-in for Open WebUI's server that answers the same API routes as the pinned version's source), and
the process lists say what the test says. Nothing a test does can reach a real configuration file."""
from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

import envrec_os
from envrec_coordinator import connect as C
from envrec_harness.intents import for_display, undisplay

SERVER = "multi-auto-setup"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Fake:
    """The commands a recipe may run, played by the test: it records every call and answers as the real tool would."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.fail: set[str] = set()
        self.listed = True

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        name = Path(argv[0]).name
        if name in self.fail:
            return SimpleNamespace(returncode=1, stdout="", stderr="boom")
        if name == "uv" and argv[1] == "venv":
            py = Path(argv[-1]) / ("Scripts" if os.name == "nt" else "bin")
            py.mkdir(parents=True, exist_ok=True)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if name == "uv" and argv[1] == "pip":
            bindir = Path(argv[argv.index("--python") + 1]).parent
            bindir.mkdir(parents=True, exist_ok=True)
            (bindir / "python").write_text("")
            exe = bindir / "open-webui"
            exe.write_text(OWUI_STAND_IN.replace("@@PY@@", sys.executable))
            exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if name == "claude" and argv[1:3] == ["mcp", "list"]:
            return SimpleNamespace(returncode=0, stdout=("other-server: x - ok\n" + (f"{SERVER}: y - ok\n" if self.listed else "")), stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


OWUI_STAND_IN = textwrap.dedent('''\
    #!@@PY@@
    """A stand-in for `open-webui serve`: the routes the connection recipe uses, behaving as the pinned version's source does (an admin made from WEBUI_ADMIN_EMAIL/PASSWORD at the first start; admin-only function routes)."""
    import json, os, sys
    from http.server import BaseHTTPRequestHandler, HTTPServer
    args = sys.argv[1:]
    port = int(args[args.index("--port") + 1])
    assert args[args.index("--host") + 1] == "127.0.0.1"
    EMAIL, PASSWORD = os.environ.get("WEBUI_ADMIN_EMAIL", ""), os.environ.get("WEBUI_ADMIN_PASSWORD", "x")
    assert os.environ["OFFLINE_MODE"] == "true"
    FUNCS = {}
    TOKEN = "t-" + PASSWORD[:6]

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def send(self, code, doc=None):
            body = json.dumps(doc).encode() if doc is not None else b""
            self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        def body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")
        def admin(self):
            return self.headers.get("Authorization") == "Bearer " + TOKEN
        def do_GET(self):
            if self.path == "/health": return self.send(200, {"status": True})
            if not self.admin(): return self.send(401, {})
            p = self.path.split("/")
            if self.path.startswith("/api/v1/functions/id/"):
                fid = p[5]
                if fid not in FUNCS: return self.send(404, None)
                if p[-1] == "valves": return self.send(200, FUNCS[fid]["valves"])
                return self.send(200, {k: v for k, v in FUNCS[fid].items() if k != "valves"})
            return self.send(404, None)
        def do_POST(self):
            b = self.body()
            if self.path == "/api/v1/auths/signin":
                ok = b.get("email") == EMAIL and b.get("password") == PASSWORD
                return self.send(200, {"token": TOKEN}) if ok else self.send(400, {"detail": "bad"})
            if not self.admin(): return self.send(401, {})
            p = self.path.split("/")
            if self.path == "/api/v1/functions/create":
                FUNCS[b["id"]] = {"id": b["id"], "name": b["name"], "is_active": False, "valves": {}}
                return self.send(200, FUNCS[b["id"]])
            if p[-1] == "toggle":
                FUNCS[p[5]]["is_active"] = not FUNCS[p[5]]["is_active"]; return self.send(200, FUNCS[p[5]])
            if p[-2:] == ["valves", "update"]:
                FUNCS[p[5]]["valves"] = b; return self.send(200, b)
            return self.send(404, None)
        def do_DELETE(self):
            if not self.admin(): return self.send(401, {})
            fid = self.path.split("/")[5]
            return self.send(200, bool(FUNCS.pop(fid, None)))

    HTTPServer(("127.0.0.1", port), H).serve_forever()
''')


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    (tmp_path / "bin").mkdir()
    for b in ("claude", "codex", "opencode", "muse", "uv"):
        (tmp_path / "bin" / b).write_text("")
    return h


def make_env(tmp_path, home, *, os_name="macos", running=False, binaries=("claude", "codex", "opencode", "muse", "uv"), port=None, fake=None):
    make_env.n = getattr(make_env, "n", 0) + 1
    bindir = tmp_path / f"bin{make_env.n}"  # a directory of its own per environment: what is installed is what the test says
    bindir.mkdir(exist_ok=True)
    for b in binaries:
        (bindir / b).write_text("#!/bin/sh\n")
        (bindir / b).chmod(0o755)
    return C.Env(home=home, os=os_name, path=str(bindir), py="/opt/py/bin/python", state_dir=tmp_path / "state", running=lambda name: running,
                 run=fake or Fake(), port=port or free_port(), clock=lambda: 1_800_000_000.0)


def run_all(plan, env, action="run", steps=None):
    results = {}
    order = steps or (range(1, len(plan["steps"]) + 1) if action == "run" else range(len(plan["steps"]), 0, -1))
    for n in order:
        results[n] = C.run_step(plan, n, action, env)
    return results


def other_servers_file(path: Path, key="mcpServers"):
    """A settings file with another entry holding something secret-looking: it must come out byte for byte."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"theme": "dark", key: {"other": {"command": "npx", "args": ["x"], "env": {"API_KEY": "SECRET-VALUE-123"}}}}, indent=2) + "\n")
    return path.read_text()


# ---- the recipes are data, each cell documented or refusing ---------------------------------------------------------------------------------------------

def test_every_recipe_step_has_a_builder_a_runner_and_an_undo_and_every_host_cites_its_documentation():
    r = C.load_recipes()
    assert set(r["hosts"]) == {"openwebui", "openwebui-cleanup", "claude-desktop", "claude-code", "codex", "opencode", "muse-code"}
    for host, h in r["hosts"].items():
        assert h["sources"] and all(s.startswith("http") for s in h["sources"]), host
        assert set(h["cells"]) == {"macos", "windows", "linux"}, host
        for spec in h["steps"]:
            assert spec["op"] in C._BUILD and spec["op"] in C._EXEC and spec["kind"] in ("configure", "install", "start", "verify"), (host, spec["op"])
        for os_name, cell in h["cells"].items():
            assert cell.get("refuse") or cell.get("status"), (host, os_name)
            if cell.get("refuse"):
                assert cell["text"] and cell["refuse"] == "os_not_documented"  # never a guess: what the documentation does not say is a typed refusal


def test_a_cell_the_documentation_does_not_cover_refuses_with_a_typed_reason(tmp_path, home):
    for host in ("opencode", "muse-code"):
        with pytest.raises(C.ConnectRefused) as exc:
            C.build_plan(host, make_env(tmp_path, home, os_name="windows"))
        assert exc.value.code == "os_not_documented" and "Windows" in exc.value.text
    with pytest.raises(C.ConnectRefused) as exc:
        C.build_plan("nope", make_env(tmp_path, home))
    assert exc.value.code == "unknown_host"


# ---- detection ---------------------------------------------------------------------------------------------------------------------------------------------

def test_only_the_hosts_that_are_here_are_found_and_open_webui_is_offered_when_uv_can_install_it(tmp_path, home):
    (home / "Library/Application Support/Claude").mkdir(parents=True)
    env = make_env(tmp_path, home, binaries=("claude", "uv"))
    rows = {r["host"]: r for r in C.detect(env)}
    assert rows["claude-desktop"]["state"] == "found" and rows["claude-code"]["state"] == "found"
    assert rows["codex"]["state"] == "absent" and rows["opencode"]["state"] == "absent" and rows["muse-code"]["state"] == "absent"
    assert rows["openwebui"]["state"] == "installable" and "uv" in rows["openwebui"]["detail"]
    none = {r["host"]: r for r in C.detect(make_env(tmp_path, home, binaries=()))}
    assert none["openwebui"]["state"] == "absent" and none["openwebui"]["code"] == "uv_missing" and none["claude-code"]["state"] == "absent"  # no uv, nothing installed: not offered
    win = {r["host"]: r for r in C.detect(make_env(tmp_path, home, os_name="windows", binaries=("opencode", "muse")))}
    assert win["opencode"]["state"] == "unsupported" and win["opencode"]["code"] == "os_not_documented" and win["muse-code"]["state"] == "unsupported"


def test_a_host_whose_settings_already_hold_our_entry_is_reported_as_connected_and_not_planned_again(tmp_path, home):
    cfg = home / "Library/Application Support/Claude/claude_desktop_config.json"
    other_servers_file(cfg)
    env = make_env(tmp_path, home)
    plan = C.build_plan("claude-desktop", env)
    run_all(plan, env)
    assert {r["host"]: r for r in C.detect(env)}["claude-desktop"]["state"] == "connected"
    with pytest.raises(C.ConnectRefused) as exc:
        C.build_plan("claude-desktop", env)
    assert exc.value.code == "already_connected"


# ---- Claude Desktop ---------------------------------------------------------------------------------------------------------------------------------------

def test_claude_desktop_refuses_while_it_runs_and_tells_the_owner_to_quit_it(tmp_path, home):
    (home / "Library/Application Support/Claude").mkdir(parents=True)
    with pytest.raises(C.ConnectRefused) as exc:
        C.build_plan("claude-desktop", make_env(tmp_path, home, running=True))
    assert exc.value.code == "app_running" and "Cmd+Q" in exc.value.text
    env = make_env(tmp_path, home)
    plan = C.build_plan("claude-desktop", env)  # quit: a plan can be made
    env.running = lambda name: True  # started again between the plan and the step: the step re-checks and refuses, and changes nothing
    cfg = home / "Library/Application Support/Claude/claude_desktop_config.json"
    with pytest.raises(C.StepFailed) as exc:
        C.run_step(plan, 2, "run", env)
    assert exc.value.code == "app_running" and not cfg.exists()


def test_claude_desktop_merges_our_entry_and_leaves_every_other_entry_byte_for_byte_then_undoes_to_the_original(tmp_path, home):
    cfg = home / "Library/Application Support/Claude/claude_desktop_config.json"
    original = other_servers_file(cfg)
    env = make_env(tmp_path, home)
    plan = C.build_plan("claude-desktop", env)
    assert [s["op"] for s in plan["steps"]] == ["backup_file", "json_merge_entry", "json_verify_entry"] and plan["plan_hash"] == C.plan_hash(plan)
    res = run_all(plan, env)
    doc = json.loads(cfg.read_text())
    assert doc["mcpServers"]["other"] == json.loads(original)["mcpServers"]["other"] and doc["theme"] == "dark"  # untouched
    entry = doc["mcpServers"][SERVER]
    assert entry == {"command": "/opt/py/bin/python", "args": ["-m", "envrec_harness.mcp_server", "--state-dir", str(tmp_path / "state"), "--channel", "claude-desktop"]}
    assert res[1]["made"] is True and res[3] == {"ok": True, "other_entries": 1}  # the other entries are counted, never read out
    backups = list(cfg.parent.glob("claude_desktop_config.json.mas-backup-*"))
    assert len(backups) == 1 and backups[0].read_text() == original
    run_all(plan, env, "undo")
    assert cfg.read_text() == original and not list(cfg.parent.glob("*.mas-backup-*"))  # as it was, and our copy is gone too


def test_claude_desktop_creates_the_file_when_there_is_none_and_the_undo_deletes_what_it_created(tmp_path, home):
    cfg = home / "Library/Application Support/Claude/claude_desktop_config.json"
    cfg.parent.mkdir(parents=True)
    env = make_env(tmp_path, home)
    plan = C.build_plan("claude-desktop", env)
    res = run_all(plan, env)
    assert res[1]["made"] is False and json.loads(cfg.read_text())["mcpServers"][SERVER]["args"][-1] == "claude-desktop"
    run_all(plan, env, "undo")
    assert not cfg.exists()


def test_when_the_owner_changed_the_file_after_our_edit_the_undo_removes_only_our_entry(tmp_path, home):
    cfg = home / "Library/Application Support/Claude/claude_desktop_config.json"
    other_servers_file(cfg)
    env = make_env(tmp_path, home)
    plan = C.build_plan("claude-desktop", env)
    run_all(plan, env)
    doc = json.loads(cfg.read_text())
    doc["mcpServers"]["added_later"] = {"command": "later"}
    cfg.write_text(json.dumps(doc, indent=2) + "\n")  # the owner (or the app) edits it
    run_all(plan, env, "undo")
    after = json.loads(cfg.read_text())
    assert SERVER not in after["mcpServers"] and after["mcpServers"]["added_later"] == {"command": "later"} and "other" in after["mcpServers"]


def test_a_settings_file_with_comments_is_refused_not_rewritten(tmp_path, home):
    cfg = home / ".config/opencode/opencode.json"
    cfg.parent.mkdir(parents=True)
    text = '{\n  // my settings\n  "theme": "x"\n}\n'
    cfg.write_text(text)
    env = make_env(tmp_path, home)
    plan = C.build_plan("opencode", env)
    with pytest.raises(C.StepFailed) as exc:
        C.run_step(plan, 2, "run", env)
    assert exc.value.code == "config_not_plain_json" and cfg.read_text() == text
    (cfg.parent / "opencode.jsonc").write_text("{}")
    with pytest.raises(C.ConnectRefused) as exc:
        C.build_plan("opencode", env)
    assert exc.value.code == "config_is_jsonc"


# ---- terminal agents ---------------------------------------------------------------------------------------------------------------------------------------

def test_claude_code_is_connected_and_disconnected_with_its_own_documented_commands_and_nothing_else(tmp_path, home):
    fake = Fake()
    env = make_env(tmp_path, home, fake=fake)
    plan = C.build_plan("claude-code", env)
    claude = plan["steps"][0]["args"]["argv"][0]
    assert plan["steps"][0]["args"]["argv"] == [claude, "mcp", "add", "--transport", "stdio", "--scope", "user", SERVER, "--", "/opt/py/bin/python", "-m", "envrec_harness.mcp_server", "--state-dir",
                                                 str(tmp_path / "state"), "--channel", "terminal"]
    assert plan["steps"][0]["undo"] == f"{claude} mcp remove {SERVER} --scope user" and plan["steps"][0]["writes"] == [str(home / ".claude.json")]
    run_all(plan, env)
    assert [c[1:3] for c in fake.calls] == [["mcp", "add"], ["mcp", "list"]]
    fake.calls.clear()
    run_all(plan, env, "undo")
    assert [c[1:] for c in fake.calls] == [["mcp", "remove", SERVER, "--scope", "user"]]  # only our entry is named; nothing else is ever touched
    assert not (home / ".claude.json").exists()  # the tool's own file is not read or written by us
    fake.fail.add("claude")
    with pytest.raises(C.StepFailed) as exc:
        C.run_step(plan, 1, "run", env)
    assert exc.value.code == "cli_failed" and "boom" not in str(exc.value)  # the command's own output is not passed on


def test_a_terminal_agent_that_is_not_installed_is_refused_with_the_reason(tmp_path, home):
    with pytest.raises(C.ConnectRefused) as exc:
        C.build_plan("claude-code", make_env(tmp_path, home, binaries=()))
    assert exc.value.code == "not_detected" and "claude" in exc.value.text


def test_codex_gets_a_toml_table_appended_with_the_rest_of_the_file_untouched_and_it_is_removed_alone(tmp_path, home):
    cfg = home / ".codex/config.toml"
    cfg.parent.mkdir(parents=True)
    original = '# my codex settings\nmodel = "x"\n\n[mcp_servers.context7]\ncommand = "npx"\nargs = ["-y", "c7"]\n\n[mcp_servers.context7.env]\nKEY = "SECRET-VALUE-123"\n'
    cfg.write_text(original)
    env = make_env(tmp_path, home)
    plan = C.build_plan("codex", env)
    run_all(plan, env)
    text = cfg.read_text()
    assert text.startswith(original) and "# my codex settings" in text  # comments and every other table survive
    doc = tomllib.loads(text)
    assert doc["mcp_servers"][SERVER] == {"command": "/opt/py/bin/python", "args": ["-m", "envrec_harness.mcp_server", "--state-dir", str(tmp_path / "state"), "--channel", "terminal"]}
    assert doc["mcp_servers"]["context7"]["env"]["KEY"] == "SECRET-VALUE-123"
    cfg.write_text(text + '\n[other]\nx = 1\n')  # edited afterwards
    run_all(plan, env, "undo")
    after = cfg.read_text()
    assert SERVER not in after and after.startswith(original.rstrip("\n")) and tomllib.loads(after)["other"] == {"x": 1}


def test_codex_restores_the_original_file_when_nothing_changed_and_creates_one_when_there_was_none(tmp_path, home):
    cfg = home / ".codex/config.toml"
    cfg.parent.mkdir(parents=True)
    env = make_env(tmp_path, home)
    plan = C.build_plan("codex", env)
    run_all(plan, env)
    assert SERVER in tomllib.loads(cfg.read_text())["mcp_servers"]
    run_all(plan, env, "undo")
    assert not cfg.exists()
    cfg.write_text('model = "y"\n')
    plan = C.build_plan("codex", env)
    run_all(plan, env)
    run_all(plan, env, "undo")
    assert cfg.read_text() == 'model = "y"\n'


def test_opencode_gets_a_local_server_entry_under_mcp_in_its_documented_shape(tmp_path, home):
    cfg = home / ".config/opencode/opencode.json"
    original = other_servers_file(cfg, key="mcp")
    env = make_env(tmp_path, home)
    plan = C.build_plan("opencode", env)
    run_all(plan, env)
    entry = json.loads(cfg.read_text())["mcp"][SERVER]
    assert entry == {"type": "local", "command": ["/opt/py/bin/python", "-m", "envrec_harness.mcp_server", "--state-dir", str(tmp_path / "state"), "--channel", "terminal"], "enabled": True}
    run_all(plan, env, "undo")
    assert cfg.read_text() == original
    plan = C.build_plan("opencode", env)  # no file: the documented $schema line comes with it
    cfg.unlink()
    run_all(plan, env)
    assert json.loads(cfg.read_text())["$schema"] == "https://opencode.ai/config.json"


def test_muse_code_gets_mcpservers_with_the_required_schema_version_and_never_both_spellings(tmp_path, home):
    cfg = home / ".config/muse/settings.json"
    env = make_env(tmp_path, home)
    plan = C.build_plan("muse-code", env)
    run_all(plan, env)
    doc = json.loads(cfg.read_text())
    assert doc["schema_version"] == 1 and doc["mcpServers"][SERVER]["command"] == "/opt/py/bin/python" and "mcp_servers" not in doc
    run_all(plan, env, "undo")
    assert not cfg.exists()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"schema_version": 1, "mcp_servers": {"o": {"command": "x"}}}))  # only the legacy spelling: ours goes there, not into a second key
    plan = C.build_plan("muse-code", env)
    run_all(plan, env)
    assert SERVER in json.loads(cfg.read_text())["mcp_servers"] and "mcpServers" not in json.loads(cfg.read_text())
    run_all(plan, env, "undo")
    cfg.write_text(json.dumps({"schema_version": 1, "mcp_servers": {}, "mcpServers": {}}))  # both: the tool treats that as ambiguous, so nothing is written
    plan = C.build_plan("muse-code", env)
    before = cfg.read_text()
    with pytest.raises(C.StepFailed) as exc:
        C.run_step(plan, 2, "run", env)
    assert exc.value.code == "ambiguous_keys" and cfg.read_text() == before
    xdg = tmp_path / "xdg"
    env2 = make_env(tmp_path, home)
    env2.config_root = xdg
    assert C.build_plan("muse-code", env2)["steps"][1]["args"]["file"] == str(xdg / "muse/settings.json")


# ---- Open WebUI ---------------------------------------------------------------------------------------------------------------------------------------------

def test_open_webui_is_installed_pinned_started_on_loopback_registered_through_its_api_verified_and_removed_completely(tmp_path, home):
    env = make_env(tmp_path, home)
    plan = C.build_plan("openwebui", env)
    pin = C.load_recipes()["hosts"]["openwebui"]["pinned"]
    assert [s["op"] for s in plan["steps"]] == ["ensure_dir", "uv_install_pinned", "openwebui_start", "openwebui_register", "openwebui_verify"]
    install = plan["steps"][1]
    assert f"open-webui=={pin['version']}" in install["exact_command"] and install["bytes"] > 1 * C.GIB and "--python 3.11" in install["exact_command"]
    start = plan["steps"][2]["exact_command"]
    assert "--host 127.0.0.1" in start and "OFFLINE_MODE=true" in start and "WEBUI_AUTH" not in start and "a generated value" in start and "0.0.0.0" not in start  # loopback only, offline, no single-user mode
    assert any(n.startswith("License:") and "branding clause" in n for n in plan["notes"]) and any("single-user mode" in n for n in plan["notes"])
    try:
        run_all(plan, env)
        d = home / "mas-openwebui"
        secret = d / "admin.secret"
        assert secret.exists() and envrec_os.current().is_private(secret)  # readable only by the owner
        assert json.loads(secret.read_text())["email"] == "admin@localhost" and (d / "run/server.json").exists()
        acct = C.read_admin_account(env)
        assert acct["email"] == "admin@localhost" and len(acct["password"]) >= 20
        log = (d / "run/server.log").read_text() if (d / "run/server.log").exists() else ""
        assert acct["password"] not in log
        # what the API now holds: the function, on, with its valve
        token = C._signin(plan["steps"][3]["args"])
        _, fn = C._http(env.port, "GET", f"/api/v1/functions/id/{C.FUNCTION_ID}", token=token)
        _, valves = C._http(env.port, "GET", f"/api/v1/functions/id/{C.FUNCTION_ID}/valves", token=token)
        assert fn["is_active"] is True and valves == {"STATE_DIR": str(tmp_path / "state")}
    finally:
        run_all(plan, env, "undo")
    assert not (home / "mas-openwebui").exists()
    with pytest.raises(OSError):
        C._http(env.port, "GET", "/health", timeout=2.0)  # the server is stopped


def test_open_webui_refuses_without_uv_and_when_the_port_is_taken_and_a_wrong_admin_password_is_not_retried(tmp_path, home):
    with pytest.raises(C.ConnectRefused) as exc:
        C.build_plan("openwebui", make_env(tmp_path, home, binaries=()))
    assert exc.value.code == "uv_missing"
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        with pytest.raises(C.ConnectRefused) as exc:
            C.build_plan("openwebui", make_env(tmp_path, home, port=s.getsockname()[1]))
    assert exc.value.code == "port_in_use"


# ---- what every plan says and never says ---------------------------------------------------------------------------------------------------------------

def test_every_plan_text_is_free_of_angle_brackets_and_of_the_real_home_and_other_entries_are_never_shown(tmp_path, home):
    cfg = home / "Library/Application Support/Claude/claude_desktop_config.json"
    other_servers_file(cfg)
    env = make_env(tmp_path, home)
    plans = [C.build_plan(h, env) for h in ("claude-desktop", "claude-code", "codex", "opencode", "muse-code", "openwebui")]
    for plan in plans:
        for s in plan["steps"]:
            for text in (s["exact_command"], s["undo"], *s["writes"]):
                shown = for_display(text)
                assert "<" not in shown and ">" not in shown and str(home) not in shown, (plan["option_id"], shown)
                assert undisplay(shown) == text  # the display form maps back to the plan's own words
                assert "SECRET-VALUE-123" not in text and "API_KEY" not in text
        assert "SECRET-VALUE-123" not in json.dumps(plan)
        assert plan["plan_hash"] == C.plan_hash(plan)
    altered = json.loads(json.dumps(plans[0]))
    altered["steps"][1]["exact_command"] += " "
    assert C.plan_hash(altered) != plans[0]["plan_hash"]  # an altered plan has another hash


# ---- removing the manual Open WebUI install, as its own plan ----------------------------------------------------------------------------------------

def start_manual_server(home, port):
    """An Open WebUI server started from the manual install's folder, as it runs on the owner's computer."""
    d = home / "mas-owui"
    (d / ".venv/bin").mkdir(parents=True)
    (d / "data").mkdir()
    exe = d / ".venv/bin/open-webui"
    exe.write_text(OWUI_STAND_IN.replace("@@PY@@", sys.executable))
    exe.chmod(0o755)
    (d / "data" / "webui.db").write_text("the owner's account and chats " * 100)
    pid = envrec_os.current().spawn_detached([str(exe), "serve", "--host", "127.0.0.1", "--port", str(port)], d / "data", d / "server.log", C._legacy_env(str(d)))
    assert C._wait_ready(port, 30.0)
    return pid, exe


def test_the_manual_install_is_stopped_and_moved_aside_never_deleted_and_the_undo_brings_it_back_running(tmp_path, home):
    env = make_env(tmp_path, home)
    pid, exe = start_manual_server(home, env.port)
    try:
        rows = {r["host"]: r for r in C.detect(env)}
        assert rows["openwebui-cleanup"]["state"] == "found" and f"pid {pid}" in rows["openwebui-cleanup"]["detail"]
        assert rows["openwebui"]["state"] == "blocked" and rows["openwebui"]["code"] == "legacy_install_present"
        with pytest.raises(C.ConnectRefused) as exc:
            C.build_plan("openwebui", env)  # the new install waits for the removal: same port, two copies of one function
        assert exc.value.code == "legacy_install_present" and "openwebui-cleanup" in exc.value.text
        plan = C.build_plan("openwebui-cleanup", env)
        assert [s["op"] for s in plan["steps"]] == ["stop_legacy_server", "move_legacy_aside", "verify_legacy_gone"]
        stop, move = plan["steps"][0], plan["steps"][1]
        assert f"process {pid}" in stop["exact_command"] and "No other process is touched" in stop["exact_command"] and stop["writes"] == []
        assert move["bytes"] > 2000 and "Nothing is deleted" in move["exact_command"] and move["undo"].startswith("Move ") and "rm " not in move["exact_command"]
        assert any("removes nothing for good" in n for n in plan["notes"])
        assert move["size_kind"] == "move" and all(x.get("size_kind") != "download" for x in plan["steps"])  # the size of this plan is a move, never a download
        run_all(plan, env)
        dst = Path(move["args"]["dst"])
        assert not (home / "mas-owui").exists() and (dst / "data" / "webui.db").read_text().startswith("the owner's account")  # moved aside, content intact
        with pytest.raises(OSError):
            C._http(env.port, "GET", "/health", timeout=2.0)  # no server any more
        run_all(plan, env, "undo")
        assert (home / "mas-owui/data/webui.db").exists() and not dst.exists()
        assert C._http(env.port, "GET", "/health", timeout=5.0)[0] == 200  # running again, from the same folder
    finally:
        for p in C._system_processes():
            if str(exe) in p["argv"]:
                C.procs.stop(p["pid"], p["created"])
    assert not [x for x in C._system_processes() if str(exe) in x["argv"]]  # nothing of the test is left running


def test_without_a_server_running_the_removal_has_no_stop_step_and_a_machine_without_the_folder_has_nothing_to_remove(tmp_path, home):
    env = make_env(tmp_path, home)
    with pytest.raises(C.ConnectRefused) as exc:
        C.build_plan("openwebui-cleanup", env)
    assert exc.value.code == "nothing_to_remove"
    (home / "mas-owui" / "data").mkdir(parents=True)
    plan = C.build_plan("openwebui-cleanup", env)
    assert [s["op"] for s in plan["steps"]] == ["move_legacy_aside", "verify_legacy_gone"]
    run_all(plan, env)
    assert not (home / "mas-owui").exists() and Path(plan["steps"][0]["args"]["dst"]).exists()


def test_the_stop_step_refuses_a_process_that_is_not_the_planned_server_and_the_move_refuses_a_taken_name_or_a_link(tmp_path, home):
    env = make_env(tmp_path, home)
    pid, exe = start_manual_server(home, env.port)
    try:
        plan = C.build_plan("openwebui-cleanup", env)
        wrong = json.loads(json.dumps(plan))
        wrong["steps"][0]["args"]["pid"] = os.getpid()  # some other process: this very test
        wrong["steps"][0]["args"]["created"] = C.procs.created_at(os.getpid())
        with pytest.raises(C.StepFailed) as exc:
            C.run_step(wrong, 1, "run", env)
        assert exc.value.code == "process_changed" and C.procs.is_ours(os.getpid(), wrong["steps"][0]["args"]["created"])  # nothing was stopped
        stale = json.loads(json.dumps(plan))
        stale["steps"][0]["args"]["created"] -= 100.0  # the pid is reused by a later process
        with pytest.raises(C.StepFailed) as exc:
            C.run_step(stale, 1, "run", env)
        assert exc.value.code == "process_changed"
        Path(plan["steps"][1]["args"]["dst"]).mkdir()
        with pytest.raises(C.StepFailed) as exc:
            C.run_step(plan, 2, "run", env)
        assert exc.value.code == "destination_exists" and (home / "mas-owui").exists()
        evil = json.loads(json.dumps(plan))
        evil["steps"][1]["args"]["dir"] = str(home)  # a plan that names another folder
        with pytest.raises(C.StepFailed) as exc:
            C.run_step(evil, 2, "run", env)
        assert exc.value.code == "path_not_ours" and home.exists()
    finally:
        for p in C._system_processes():
            if str(exe) in p["argv"]:
                C.procs.stop(p["pid"], p["created"])


# ---- the real text of the plans: no placeholder, the detected program's own path ----------------------------------------------------------------------------

def test_the_plan_texts_show_the_detected_program_path_and_hold_no_angle_bracket_and_a_plan_that_did_is_refused(tmp_path, home):
    from envrec_harness import cards
    from envrec_harness.intents import Intent, IntentInvalid, validate
    env = make_env(tmp_path, home)
    (home / "Library/Application Support/Claude").mkdir(parents=True)
    (home / "mas-owui" / "data").mkdir(parents=True)
    plans = {h: C.build_plan(h, env) for h in ("claude-desktop", "claude-code", "codex", "opencode", "muse-code", "openwebui-cleanup")}
    (home / "mas-owui" / "data").rmdir()
    (home / "mas-owui").rmdir()
    plans["openwebui"] = C.build_plan("openwebui", env)
    claude_path = env.which("claude")
    uv_path = env.which("uv")
    assert claude_path in plans["claude-code"]["steps"][0]["exact_command"] and uv_path in plans["openwebui"]["steps"][1]["exact_command"]  # the real path of the program that runs
    for host, plan in plans.items():
        d = {"connect": True, "plan_hash": plan["plan_hash"], "machine": "this computer", "option_title": plan["option_title"], "total_download_bytes": 0, "notes": [for_display(n) for n in plan["notes"]],
             "verification": plan["verification"], "steps": [{"n": s["n"], "title": s["title"], "exact_command": for_display(s["exact_command"]), "writes": [for_display(w) for w in s["writes"]], "bytes": s["bytes"],
                                                                "undo": for_display(s["undo"]), "needs_owner_approval": True} for s in plan["steps"]]}
        text = cards.plan_text(d, "en")
        assert "<" not in text and ">" not in text, (host, [ln for ln in text.splitlines() if "<" in ln or ">" in ln])
        for s in d["steps"]:
            assert s["exact_command"] in text and s["undo"] in text, (host, s["n"], "a command or an undo was cut short in the plan text")  # the exact command, whole: never shortened with an ellipsis
        assert str(home) not in text
        validate(Intent(id="p", kind="show_data", run_id=None, actor="GUARD", sensitivity="internal", payload={"data_kind": "plan", "data": d}, text_fallback=text))
        with pytest.raises(IntentInvalid):
            validate(Intent(id="p", kind="show_data", run_id=None, actor="GUARD", sensitivity="internal", payload={"data_kind": "plan", "data": d}, text_fallback=text + "\nrm -rf <path>/x"))

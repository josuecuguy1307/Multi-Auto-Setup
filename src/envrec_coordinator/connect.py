"""Connecting a chat host to Multi Auto Setup, as a setup like any other: a plan built from data (`skills/connect-recipes`), shown to the owner with its exact commands, what each step writes and how to undo it, and run
one step at a time only after the owner approves that step with a human gesture. This module is the generic part: it reads the recipes, detects which hosts are on this computer, builds the plan, and runs a step.
It never names a host in code (the recipes do), never reads a secret value (a settings file is parsed in memory to merge one entry and is never logged, shown or sent), never touches an entry that is not ours,
and refuses with a typed reason what it cannot do safely.

The steps run on THIS computer (the one the coordinator runs on, where the owner's chats are), by the coordinator's own code after an OWNER approval bound to the plan hash, step and action. Tests give it a
temporary home and fake commands: nothing here is ever run against the owner's real configuration by a test."""

from __future__ import annotations

import hashlib
import http.client
import json
import re
import secrets
import shutil
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import psutil

import envrec_os
from envrec_os import SKILLS_DIR, procs

RECIPES_FILE = SKILLS_DIR / "connect-recipes" / "references" / "recipes.json"
OPENWEBUI_DIR = "mas-openwebui"
FUNCTION_ID = "multi_auto_setup"
ADMIN_EMAIL = "admin@localhost"
GIB = 2 ** 30


class ConnectRefused(Exception):
    """No plan can be made, and why: `code` is a typed reason (not_detected, app_running, config_is_jsonc, os_not_documented, ...), `text` says it in words."""

    def __init__(self, code: str, text: str) -> None:
        super().__init__(f"{code}: {text}")
        self.code, self.text = code, text


class StepFailed(Exception):
    def __init__(self, code: str, text: str = "") -> None:
        super().__init__(f"{code}: {text}")
        self.code, self.text = code, text


def _running(name: str) -> bool:
    try:
        return any(((p.info.get("name") or "") == name) for p in psutil.process_iter(["name"]))
    except psutil.Error:
        return False


@dataclass
class Env:
    """What the steps need to know about this computer. Tests build one with a temporary home and fake commands; `system()` is the real one."""

    home: Path
    os: str  # macos | windows | linux
    path: str  # where to look for a program
    py: str  # the interpreter our MCP server entry runs with
    state_dir: Path  # the coordinator's state directory (the entry says where to find it)
    config_root: Path | None = None  # where this user's application settings live (the per-OS layer says); None means ask it
    port: int = 8080
    running: Callable[[str], bool] = _running
    run: Callable[..., Any] = subprocess.run
    processes: Callable[[], list[dict]] | None = None  # (pid, created, argv) of what runs here: a test says what runs; None means ask the system
    clock: Callable[[], float] = time.time

    @classmethod
    def system(cls, state_dir: Path) -> "Env":
        import os
        fam = {"darwin": "macos"}.get(envrec_os.family(), envrec_os.family())
        return cls(home=Path.home(), os=fam, path=os.environ.get("PATH", ""), py=sys.executable, state_dir=Path(state_dir).resolve(), config_root=envrec_os.config_root(Path.home()))

    def fill(self, text: str, **extra: str) -> str:
        """A recipe's path or command with its placeholders written out for this computer."""
        s = str(text)
        if s == "~" or s.startswith("~/"):
            s = str(self.home) + s[1:]
        s = s.replace("{config_root}", str(self.config_root or envrec_os.config_root(self.home, {})))
        s = s.replace("{py}", self.py).replace("{state_dir}", str(self.state_dir))
        for k, v in extra.items():
            s = s.replace("{" + k + "}", v)
        return s

    def which(self, binary: str) -> str | None:
        return shutil.which(binary, path=self.path)


def load_recipes(path: Path = RECIPES_FILE) -> dict:
    return json.loads(path.read_text())


def plan_hash(plan: dict) -> str:
    body = {k: v for k, v in plan.items() if k != "plan_hash"}
    return "sha256:" + hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def sha_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


LEGACY_DIR = "mas-owui"  # the folder of the manual Open WebUI install made in an earlier session


def _system_processes() -> list[dict]:
    out = []
    try:
        for p in psutil.process_iter(["pid", "create_time", "cmdline"]):
            argv = p.info.get("cmdline") or []
            if any("open-webui" in a for a in argv):  # only the Open WebUI servers are looked at: no other process's command line is read into anything
                out.append({"pid": p.info["pid"], "created": p.info["create_time"], "argv": list(argv)})
    except psutil.Error:
        pass
    return out


def legacy_openwebui(env: Env) -> dict:
    """The manual Open WebUI install from an earlier session (read-only): its folder, and the servers that were started from it (their pid, start time and port)."""
    d = env.home / LEGACY_DIR
    exe = str(d / ".venv" / ("Scripts" if env.os == "windows" else "bin") / ("open-webui.exe" if env.os == "windows" else "open-webui"))
    procs_ = []
    for p in (env.processes or _system_processes)():
        if exe in p["argv"] and "serve" in p["argv"]:
            port = int(p["argv"][p["argv"].index("--port") + 1]) if "--port" in p["argv"] else 8080
            procs_.append({"pid": p["pid"], "created": p["created"], "port": port})
    return {"dir": str(d) if d.exists() and not d.is_symlink() else None, "exe": exe, "processes": procs_}


# ---- detection (read-only) ------------------------------------------------------------------------------------------------------------------------------

def cell_of(recipes: dict, host: str, env: Env) -> dict:
    h = recipes["hosts"].get(host)
    if h is None:
        raise ConnectRefused("unknown_host", f"there is no recipe for a chat called {host!r}")
    cell = h["cells"].get(env.os)
    if cell is None:
        raise ConnectRefused("os_not_documented", f"{h['label']} has no recipe for this operating system")
    if cell.get("refuse"):
        raise ConnectRefused(cell["refuse"], cell["text"])
    return cell


def config_of(recipes: dict, host: str, env: Env) -> Path | None:
    cell = cell_of(recipes, host, env)
    return Path(env.fill(cell["config"])) if cell.get("config") else None


def detect(env: Env, recipes: dict | None = None) -> list[dict]:
    """Which of the chats are on this computer: `found` (there), `installable` (Open WebUI, when `uv` is there to install it), `connected` (our entry is already in its settings), `unsupported` (this operating
    system is not covered by the tool's documentation, with the reason) or `absent`. Reads no value of any settings file; looks for our entry's name only."""
    r = recipes or load_recipes()
    out = []
    for host, h in r["hosts"].items():
        row: dict[str, Any] = {"host": host, "label": h["label"], "state": "absent", "detail": "", "running": False}
        try:
            cell = cell_of(r, host, env)
        except ConnectRefused as exc:
            row.update(state="unsupported", detail=exc.text, code=exc.code)
            out.append(row)
            continue
        if h.get("kind") == "cleanup":
            lg = legacy_openwebui(env)
            if lg["dir"]:
                row.update(state="found", detail=f"~/{LEGACY_DIR}" + (f", running (pid {lg['processes'][0]['pid']})" if lg["processes"] else ", not running"))
        elif host == "openwebui":
            if legacy_openwebui(env)["dir"]:
                row.update(state="blocked", code="legacy_install_present", detail=f"a manual install is in ~/{LEGACY_DIR}: remove it first with its own plan")
            elif (env.home / OPENWEBUI_DIR / "venv").exists():
                row.update(state="found", detail=f"installed in ~/{OPENWEBUI_DIR}")
            elif env.which("uv"):
                row.update(state="installable", detail="not installed; it can be installed here (uv was found)")
            else:
                row.update(code="uv_missing", detail="uv was not found, and it installs Open WebUI")
        elif h.get("process") and cell.get("app_dir"):
            if Path(env.fill(cell["app_dir"])).exists():
                row.update(state="found", detail="its settings folder is here", running=bool(env.running(h["process"][env.os])))
        elif h.get("binary") and env.which(h["binary"]):
            row.update(state="found", detail=f"the command {h['binary']} was found")
        if row["state"] == "found" and cell.get("config"):
            cfg = Path(env.fill(cell["config"]))
            if cfg.exists() and r["server_name"] in _entry_names(cfg):
                row["state"] = "connected"
                row["detail"] += "; our entry is already in its settings"
        out.append(row)
    return out


def _entry_names(cfg: Path) -> set[str]:
    """Only the NAMES of the entries in a settings file (never a value), to see whether ours is there. A file that cannot be parsed says nothing."""
    try:
        text = cfg.read_text()
        if cfg.suffix == ".toml":
            return set((tomllib.loads(text).get("mcp_servers") or {}))
        doc = json.loads(text)
        names: set[str] = set()
        for key in ("mcpServers", "mcp_servers", "mcp"):
            if isinstance(doc.get(key), dict):
                names |= set(doc[key])
        return names
    except (OSError, ValueError, AttributeError):
        return set()


# ---- the plan --------------------------------------------------------------------------------------------------------------------------------------------

def _entry(template: Any, r: dict, env: Env, channel: str) -> Any:
    """A recipe's entry with its placeholders filled; `@entry_args` stands for the argument list of our server."""
    args = [env.fill(a, channel=channel) for a in r["entry_args"]]
    if isinstance(template, dict):
        return {k: _entry(v, r, env, channel) for k, v in template.items()}
    if isinstance(template, list):
        out: list = []
        for v in template:
            if v == "@entry_args":
                out += args
            else:
                out.append(_entry(v, r, env, channel))
        return out
    if template == "@entry_args":
        return args
    return env.fill(template, channel=channel) if isinstance(template, str) else template


def build_plan(host: str, env: Env, recipes: dict | None = None) -> dict:
    """The plan to connect `host` on this computer, or a typed refusal. Every step carries its exact command, what it writes, its size, its undo and the arguments the executor will use; the hash covers all of it."""
    r = recipes or load_recipes()
    h = r["hosts"].get(host)
    if h is None:
        raise ConnectRefused("unknown_host", f"there is no recipe for a chat called {host!r}")
    cell = cell_of(r, host, env)
    channel, name = h["channel"], r["server_name"]
    notes: list[str] = []
    ctx: dict[str, Any] = {"backup": None}
    legacy = legacy_openwebui(env) if h.get("kind") == "cleanup" else None
    if legacy is not None:
        if not legacy["dir"]:
            raise ConnectRefused("nothing_to_remove", f"there is no manual Open WebUI install in ~/{LEGACY_DIR}")
        ctx["legacy"] = legacy
        notes.append(f"This removes nothing for good: the folder is moved aside to a new name and the server is stopped; you can delete the moved folder yourself when you are sure. Its Open WebUI account and chats are in that folder.")
    elif host == "openwebui" and legacy_openwebui(env)["dir"]:
        raise ConnectRefused("legacy_install_present", f"a manual Open WebUI install is in ~/{LEGACY_DIR} and would clash with this one (the same port, and two copies of the same function). Remove it first with its own plan: connect \"openwebui-cleanup\"")
    elif h.get("process"):
        if not Path(env.fill(cell["app_dir"])).exists():
            raise ConnectRefused("not_detected", f"{h['label']}'s settings folder was not found on this computer")
        if env.running(h["process"][env.os]):
            raise ConnectRefused("app_running", f"{h['label']} is running. {h['quits_to_edit']}")
        notes.append(h["quits_to_edit"])
    elif host != "openwebui" and legacy is None:
        if not env.which(h["binary"]):
            raise ConnectRefused("not_detected", f"the command {h['binary']} was not found on this computer")
        notes.append(f"Start a new {h['label']} session afterwards so that it loads the server.")
    cfg = Path(env.fill(cell["config"])) if cell.get("config") else None
    if cfg is not None:
        sib = cell.get("jsonc_sibling")
        if sib and Path(env.fill(sib)).exists():
            raise ConnectRefused("config_is_jsonc", f"{Path(env.fill(sib)).name} exists: it may hold comments, and a file with comments is not rewritten here. Add the entry by hand or remove the comments")
        if cfg.exists() and name in _entry_names(cfg):
            raise ConnectRefused("already_connected", f"our entry is already in {cfg.name}")
        t = time.localtime(env.clock())
        stamp = f"{t.tm_year:04d}{t.tm_mon:02d}{t.tm_mday:02d}-{t.tm_hour:02d}{t.tm_min:02d}{t.tm_sec:02d}"
        ctx["backup"] = str(cfg) + f".mas-backup-{stamp}"
    steps: list[dict] = []
    for spec in h["steps"]:
        if spec.get("only_if") == "legacy_process" and not (ctx.get("legacy") or {}).get("processes"):
            continue  # nothing runs from that folder: there is nothing to stop
        b = _BUILD.get(spec["op"])
        if b is None:
            raise ConnectRefused("unknown_op", f"the recipe uses an operation this version does not have: {spec['op']}")
        steps.append({"n": len(steps) + 1, "op": spec["op"], "kind": spec["kind"], "title": spec["title"], "needs_owner_approval": True, "status": "pending", **b(h, cell, spec, env, r, channel, ctx, cfg)})
    if host == "openwebui":
        pin = h["pinned"]
        if not env.which("uv") and not (env.home / OPENWEBUI_DIR / "venv").exists():
            raise ConnectRefused("uv_missing", "uv was not found: it creates the Python environment for Open WebUI")
        if _port_busy(env.port):
            raise ConnectRefused("port_in_use", f"port {env.port} on this computer is already in use")
        notes.append(f"License: {h['license_exception']}")
        notes.append(f"Open WebUI {pin['version']} (pinned) needs about {pin['disk_gib']} GiB of disk. The first admin account is created from a generated password. It is shown to you once, in your terminal or on your own screen and never in a chat, and kept in ~/{OPENWEBUI_DIR}/admin.secret, readable only by you (a later version will keep it in the operating system's keychain); "
                     "single-user mode (no password) is not used: with Open WebUI's default settings any web page could sign in as its administrator, who can run code.")
    if cell.get("status") == "untested":
        notes.append(f"This recipe has not been run on this computer yet ({env.os}).")
    plan = {"plan_id": f"connect-{host}-{int(env.clock())}", "machine_id": "this computer", "option_id": host, "option_title": h["label"], "sensitivity": "internal", "recipe_id": f"connect:{host}", "channel": channel,
            "fit": {}, "verification": {"what": h["verification"]}, "plan_title": h.get("plan_title"), "sources": h["sources"], "notes": notes, "steps": steps}
    plan["plan_hash"] = plan_hash(plan)
    return plan


def _port_busy(port: int) -> bool:
    import socket
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _pretty(entry: Any) -> str:
    return json.dumps(entry, ensure_ascii=False)


def _b_backup(h, cell, spec, env, r, channel, ctx, cfg) -> dict:
    return {"exact_command": f"If {cfg} exists, copy it to {ctx['backup']} (nothing is copied when there is no file yet)", "writes": [ctx["backup"]], "bytes": 0,
            "undo": f"Delete {ctx['backup']} if this plan made it", "args": {"file": str(cfg), "backup": ctx["backup"]}}


def _b_json_merge(h, cell, spec, env, r, channel, ctx, cfg) -> dict:
    entry = _entry(spec["entry"], r, env, channel)
    name = r["server_name"]
    return {"exact_command": f"In {cfg}, under \"{spec['key']}\", add the entry {_pretty({name: entry})}. Every other entry is left exactly as it is. If the file does not exist it is created",
            "writes": [str(cfg)], "bytes": 0,
            "undo": f"Remove the entry {name} under \"{spec['key']}\" in {cfg}. If the file is otherwise unchanged since this step, put the copy back instead (or delete the file if this step created it)",
            "args": {"file": str(cfg), "key": spec["key"], "legacy_key": spec.get("legacy_key"), "name": name, "entry": entry, "new_file": spec.get("new_file") or {}, "backup": ctx["backup"],
                     "jsonc_sibling": env.fill(cell["jsonc_sibling"]) if cell.get("jsonc_sibling") else None}}


def _b_json_verify(h, cell, spec, env, r, channel, ctx, cfg) -> dict:
    return {"exact_command": f"Read {cfg} back and check that the entry {r['server_name']} is there and is what this plan wrote (the other entries are only counted, never read out)", "writes": [], "bytes": 0,
            "undo": "nothing to undo", "args": {"file": str(cfg), "key": spec.get("key"), "name": r["server_name"]}}


def _b_toml_add(h, cell, spec, env, r, channel, ctx, cfg) -> dict:
    entry = _entry(spec["entry"], r, env, channel)
    name = r["server_name"]
    return {"exact_command": f"Append to {cfg} the table [{spec['table']}.{name}] with {_pretty(entry)}. Everything already in the file stays as it is. If the file does not exist it is created",
            "writes": [str(cfg)], "bytes": 0,
            "undo": f"Remove only the table [{spec['table']}.{name}] from {cfg}. If the file is otherwise unchanged since this step, put the copy back instead (or delete the file if this step created it)",
            "args": {"file": str(cfg), "table": spec["table"], "name": name, "entry": entry, "backup": ctx["backup"]}}


def _b_toml_verify(h, cell, spec, env, r, channel, ctx, cfg) -> dict:
    return {"exact_command": f"Read {cfg} back and check that the table {r['server_name']} is there and is what this plan wrote", "writes": [], "bytes": 0, "undo": "nothing to undo",
            "args": {"file": str(cfg), "table": "mcp_servers", "name": r["server_name"]}}


def _fill_argv(argv: list, r: dict, env: Env, channel: str, binary: str) -> list[str]:
    out: list[str] = []
    for a in argv:
        if a == "@entry_args":
            out += [env.fill(x, channel=channel) for x in r["entry_args"]]
        else:
            out.append(env.fill(a, channel=channel, binary=binary))
    return out


def _b_cli_add(h, cell, spec, env, r, channel, ctx, cfg) -> dict:
    binary = env.which(h["binary"]) or h["binary"]
    argv, undo = _fill_argv(spec["argv"], r, env, channel, binary), _fill_argv(spec["undo_argv"], r, env, channel, binary)
    return {"exact_command": " ".join(argv), "writes": [env.fill(w) for w in spec.get("writes", [])], "bytes": 0, "undo": " ".join(undo), "args": {"argv": argv, "undo_argv": undo}}


def _b_cli_verify(h, cell, spec, env, r, channel, ctx, cfg) -> dict:
    binary = env.which(h["binary"]) or h["binary"]
    argv = _fill_argv(spec["argv"], r, env, channel, binary)
    return {"exact_command": f"{' '.join(argv)} (only the line of {spec['name']} is looked at)", "writes": [], "bytes": 0, "undo": "nothing to undo", "args": {"argv": argv, "name": spec["name"]}}


def _owui(env: Env) -> dict:
    d = env.home / OPENWEBUI_DIR
    venv_py = d / "venv" / ("Scripts" if env.os == "windows" else "bin") / ("python.exe" if env.os == "windows" else "python")
    exe = d / "venv" / ("Scripts" if env.os == "windows" else "bin") / ("open-webui.exe" if env.os == "windows" else "open-webui")
    return {"dir": str(d), "venv": str(d / "venv"), "venv_py": str(venv_py), "exe": str(exe), "data": str(d / "data"), "run": str(d / "run"), "secret": str(d / "admin.secret"), "port": env.port}


def _b_ensure_dir(h, cell, spec, env, r, channel, ctx, cfg) -> dict:
    o = _owui(env)
    return {"exact_command": f"Create the folder {o['dir']} with its data and run folders, readable only by you", "writes": [o["dir"] + "/"], "bytes": 0,
            "undo": f"Delete the folder {o['dir']} and everything in it (the environment, Open WebUI's data and the secret of the admin account)", "args": o}


def _b_uv_install(h, cell, spec, env, r, channel, ctx, cfg) -> dict:
    o, pin = _owui(env), h["pinned"]
    uv = env.which("uv") or "uv"
    a1 = [uv, "venv", "--python", pin["python"], o["venv"]]
    a2 = [uv, "pip", "install", "--python", o["venv_py"], f"{pin['package']}=={pin['version']}"]
    return {"exact_command": " ".join(a1) + "  then  " + " ".join(a2), "writes": [o["venv"] + "/"], "bytes": int(pin["disk_gib"] * GIB), "size_kind": "download", "undo": f"Delete the folder {o['venv']}", "args": {**o, "argv": [a1, a2]}}


def _owui_env(o: dict) -> dict[str, str]:
    return {"OFFLINE_MODE": "true", "ENABLE_VERSION_UPDATE_CHECK": "false", "ENABLE_OLLAMA_API": "false", "ENABLE_OPENAI_API": "false", "ENABLE_COMMUNITY_SHARING": "false", "DATA_DIR": o["data"]}


def _b_owui_start(h, cell, spec, env, r, channel, ctx, cfg) -> dict:
    o = _owui(env)
    shown = " ".join(f"{k}={v}" for k, v in _owui_env(o).items())
    return {"exact_command": f"In {o['data']}, start in the background: {o['exe']} serve --host 127.0.0.1 --port {o['port']}  with  {shown}  and the variables WEBUI_ADMIN_EMAIL ({ADMIN_EMAIL}) and WEBUI_ADMIN_PASSWORD (a generated value), so that its first start creates "
                             f"the admin account. The password is generated when this step runs and kept in {o['secret']}",
            "writes": [o["data"] + "/", o["run"] + "/", o["secret"]], "bytes": 0,
            "undo": f"Stop the Open WebUI process this step started (its process id is in {o['run']}/server.json)", "args": o}


def _b_owui_register(h, cell, spec, env, r, channel, ctx, cfg) -> dict:
    o = _owui(env)
    pipe = _pipe_file()
    return {"exact_command": f"With Open WebUI's own API on 127.0.0.1:{o['port']}, signed in as the admin account of the step before: POST /api/v1/functions/create with the packaged Pipe (envrec_harness/openwebui_pipe.py, "
                             f"sha256 {sha_of(pipe)}) as the function {FUNCTION_ID}; POST /api/v1/functions/id/{FUNCTION_ID}/toggle to turn it on; POST /api/v1/functions/id/{FUNCTION_ID}/valves/update with STATE_DIR={env.state_dir}",
            "writes": [o["data"] + "/ (Open WebUI's own database)"], "bytes": 0,
            "undo": f"DELETE /api/v1/functions/id/{FUNCTION_ID}/delete on Open WebUI's API (signed in with the saved admin account)", "args": {**o, "state_dir": str(env.state_dir), "pipe": str(pipe), "pipe_sha256": sha_of(pipe)}}


def _b_owui_verify(h, cell, spec, env, r, channel, ctx, cfg) -> dict:
    o = _owui(env)
    return {"exact_command": f"GET /api/v1/functions/id/{FUNCTION_ID} and /valves on 127.0.0.1:{o['port']} and check that the function is on and its valve STATE_DIR is {env.state_dir}", "writes": [], "bytes": 0, "undo": "nothing to undo",
            "args": {**o, "state_dir": str(env.state_dir)}}


def _legacy_env(d: str) -> dict[str, str]:
    return {"OFFLINE_MODE": "true", "ENABLE_VERSION_UPDATE_CHECK": "false", "ENABLE_OLLAMA_API": "false", "ENABLE_OPENAI_API": "false", "ENABLE_COMMUNITY_SHARING": "false", "DATA_DIR": str(Path(d) / "data")}


def _dir_bytes(d: Path) -> int:
    import os
    total = 0
    for root, _dirs, files in os.walk(d):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def _b_stop_legacy(h, cell, spec, env, r, channel, ctx, cfg) -> dict:
    lg = ctx["legacy"]
    p = lg["processes"][0]
    cmd = f"{lg['exe']} serve --host 127.0.0.1 --port {p['port']}"
    return {"exact_command": f"Stop the process {p['pid']}, the Open WebUI server started from {lg['dir']} (it listens on 127.0.0.1:{p['port']}), and the programs it started. No other process is touched",
            "writes": [], "bytes": 0,
            "undo": f"Start it again from {lg['dir']}/data: {cmd} with OFFLINE_MODE=true ENABLE_VERSION_UPDATE_CHECK=false ENABLE_OLLAMA_API=false ENABLE_OPENAI_API=false ENABLE_COMMUNITY_SHARING=false DATA_DIR={lg['dir']}/data",
            "args": {"dir": lg["dir"], "exe": lg["exe"], "pid": p["pid"], "created": p["created"], "port": p["port"]}}


def _b_move_aside(h, cell, spec, env, r, channel, ctx, cfg) -> dict:
    lg = ctx["legacy"]
    t = env.clock()
    lt = time.localtime(t)
    dst = lg["dir"] + f".removed-{lt.tm_year:04d}{lt.tm_mon:02d}{lt.tm_mday:02d}-{lt.tm_hour:02d}{lt.tm_min:02d}{lt.tm_sec:02d}"
    size = _dir_bytes(Path(lg["dir"]))
    return {"exact_command": f"Move the folder {lg['dir']} (its environment, Open WebUI's data with its account and chats, and its log) to {dst}. Nothing is deleted", "writes": [lg["dir"] + "/", dst + "/"], "bytes": size, "size_kind": "move",
            "undo": f"Move {dst} back to {lg['dir']}", "args": {"dir": lg["dir"], "dst": dst, "port": ctx["legacy"]["processes"][0]["port"] if ctx["legacy"]["processes"] else 8080}}


def _b_verify_gone(h, cell, spec, env, r, channel, ctx, cfg) -> dict:
    lg = ctx["legacy"]
    return {"exact_command": f"Check that {lg['dir']} is gone from its place, that the moved folder is there, and that no Open WebUI server started from it is running", "writes": [], "bytes": 0, "undo": "nothing to undo",
            "args": {"dir": lg["dir"], "exe": lg["exe"]}}


def _pipe_file() -> Path:
    import envrec_harness
    return Path(envrec_harness.__file__).parent / "openwebui_pipe.py"


_BUILD = {"stop_legacy_server": _b_stop_legacy, "move_legacy_aside": _b_move_aside, "verify_legacy_gone": _b_verify_gone, "backup_file": _b_backup, "json_merge_entry": _b_json_merge, "json_verify_entry": _b_json_verify, "toml_add_table": _b_toml_add, "toml_verify_table": _b_toml_verify, "cli_add": _b_cli_add,
          "cli_verify_listed": _b_cli_verify, "ensure_dir": _b_ensure_dir, "uv_install_pinned": _b_uv_install, "openwebui_start": _b_owui_start, "openwebui_register": _b_owui_register, "openwebui_verify": _b_owui_verify}


# ---- running a step ----------------------------------------------------------------------------------------------------------------------------------------

class Store:
    """What a later step or an undo needs to remember (whether the file existed, the hash after our edit). Names and hashes only: no content of any settings file."""

    def __init__(self, state_dir: Path, host: str) -> None:
        self.path = Path(state_dir) / "connects" / f"{host}.json"

    def load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def save(self, doc: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc))
        tmp.replace(self.path)


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else None
    tmp = path.with_name(path.name + ".mas-tmp")
    tmp.write_text(text)
    if mode is not None:
        tmp.chmod(mode)
    tmp.replace(path)


def _check_closed(env: Env, plan_host: dict, step: dict) -> None:
    """Re-checked when the step runs, not only when the plan was made: an app that was started in between must not have its file edited under it."""
    proc = plan_host.get("process")
    if proc and env.running(proc[env.os]):
        raise StepFailed("app_running", plan_host["quits_to_edit"])


def _x_backup(a, env, store, host, h, undo=False):
    f, b = Path(a["file"]), Path(a["backup"])
    st = store.load()
    if undo:
        if st.get("backup_made") and b.exists():
            b.unlink()
        return {"removed": bool(st.get("backup_made"))}
    st.update(existed=f.exists(), backup_made=False)
    if f.exists():
        shutil.copy2(f, b)
        st["backup_made"] = True
    store.save(st)
    return {"made": st["backup_made"], "file_existed": st["existed"]}


def _restore_or_remove(a, store, remove_entry: Callable[[], None]) -> dict:
    """The undo of an edit of a settings file: restore the copy when the file is as we left it, delete a file we created, otherwise remove only our entry."""
    f, b = Path(a["file"]), Path(a["backup"])
    st = store.load()
    if not f.exists():
        return {"how": "nothing_there"}
    unchanged = bool(st.get("sha_after")) and sha_of(f) == st["sha_after"]
    if unchanged and st.get("backup_made") and b.exists():
        shutil.copy2(b, f)
        return {"how": "restored_copy"}
    if unchanged and st.get("existed") is False:
        f.unlink()
        return {"how": "deleted_file_we_created"}
    remove_entry()
    return {"how": "removed_our_entry"}


def _x_json_merge(a, env, store, host, h, undo=False):
    f = Path(a["file"])
    _check_closed(env, h, a)
    if undo:
        def drop():
            doc = _load_json(f)
            for key in (a["key"], a.get("legacy_key")):
                if key and isinstance(doc.get(key), dict):
                    doc[key].pop(a["name"], None)
            _write_atomic(f, json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
        return _restore_or_remove(a, store, drop)
    if a.get("jsonc_sibling") and Path(a["jsonc_sibling"]).exists():
        raise StepFailed("config_is_jsonc", "a settings file with comments is not rewritten here")
    doc = _load_json(f) if f.exists() else dict(a.get("new_file") or {})
    key = a["key"]
    if a.get("legacy_key") and a["legacy_key"] in doc:
        if key in doc:
            raise StepFailed("ambiguous_keys", f"the file has both {key} and {a['legacy_key']}: the tool treats that as ambiguous, so nothing was changed")
        key = a["legacy_key"]
    section = doc.setdefault(key, {})
    if not isinstance(section, dict):
        raise StepFailed("config_unexpected_shape", f"\"{key}\" in the settings file is not an object")
    if a["name"] in section and section[a["name"]] != a["entry"]:
        raise StepFailed("entry_exists_different", "an entry with our name is already there and differs: it was not touched")
    section[a["name"]] = a["entry"]
    _write_atomic(f, json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    st = store.load()
    st["sha_after"] = sha_of(f)
    store.save(st)
    return {"written": True, "key": key}


def _load_json(f: Path) -> dict:
    try:
        doc = json.loads(f.read_text())
    except ValueError as exc:
        raise StepFailed("config_not_plain_json", "the settings file is not plain JSON (comments or a syntax error): it was not changed") from exc
    except OSError as exc:
        raise StepFailed("config_unreadable", type(exc).__name__) from exc
    if not isinstance(doc, dict):
        raise StepFailed("config_unexpected_shape", "the settings file is not a JSON object")
    return doc


def _x_json_verify(a, env, store, host, h, undo=False):
    if undo:
        return {}
    doc = _load_json(Path(a["file"]))
    for key in (a["key"], "mcp_servers"):
        section = doc.get(key)
        if isinstance(section, dict) and a["name"] in section:
            return {"ok": True, "other_entries": len(section) - 1}
    raise StepFailed("entry_missing", "our entry was not found when the file was read back")


def _toml_block(a) -> str:
    e = a["entry"]
    return f"[{a['table']}.{a['name']}]\ncommand = {json.dumps(e['command'])}\nargs = {json.dumps(e['args'])}\n"


def _x_toml_add(a, env, store, host, h, undo=False):
    f = Path(a["file"])
    if undo:
        def drop():
            lines = f.read_text().splitlines(keepends=True)
            header = f"[{a['table']}.{a['name']}]"
            start = next((i for i, ln in enumerate(lines) if ln.strip() == header), None)
            if start is None:
                return
            end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
            if start > 0 and lines[start - 1].strip() == "":
                start -= 1  # the blank line this step put before the table
            text = "".join(lines[:start] + lines[end:])
            tomllib.loads(text)  # what is left must still be valid
            _write_atomic(f, text)
        return _restore_or_remove(a, store, drop)
    old = f.read_text() if f.exists() else ""
    if old:
        try:
            doc = tomllib.loads(old)
        except tomllib.TOMLDecodeError as exc:
            raise StepFailed("config_not_valid_toml", "the settings file is not valid TOML: it was not changed") from exc
        if a["name"] in (doc.get(a["table"]) or {}):
            raise StepFailed("entry_exists", "a table with our name is already there: it was not touched")
    text = old + ("" if not old or old.endswith("\n") else "\n") + ("\n" if old else "") + _toml_block(a)
    tomllib.loads(text)
    _write_atomic(f, text)
    st = store.load()
    st["sha_after"] = sha_of(f)
    store.save(st)
    return {"written": True}


def _x_toml_verify(a, env, store, host, h, undo=False):
    if undo:
        return {}
    try:
        doc = tomllib.loads(Path(a["file"]).read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise StepFailed("config_unreadable", type(exc).__name__) from exc
    section = doc.get(a["table"]) or {}
    if a["name"] in section:
        return {"ok": True, "other_entries": len(section) - 1}
    raise StepFailed("entry_missing", "our table was not found when the file was read back")


def _run_cli(env: Env, argv: list[str], timeout: float = 120.0):
    try:
        done = env.run(argv, capture_output=True, text=True, timeout=timeout, env={**envrec_os.child_environment(), "PATH": env.path, "HOME": str(env.home)})
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StepFailed("cli_failed", type(exc).__name__) from exc
    return done


def _x_cli_add(a, env, store, host, h, undo=False):
    done = _run_cli(env, a["undo_argv"] if undo else a["argv"])
    if done.returncode != 0:
        raise StepFailed("cli_failed", f"the command ended with code {done.returncode}")
    return {"ran": True}


def _x_cli_verify(a, env, store, host, h, undo=False):
    if undo:
        return {}
    done = _run_cli(env, a["argv"], 60.0)
    listed = any(ln.strip().startswith(a["name"]) for ln in (done.stdout or "").splitlines())  # only our own line is looked at; the rest of the output is dropped here
    if done.returncode != 0 or not listed:
        raise StepFailed("entry_missing", "the command did not list our server")
    return {"ok": True}


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return not path.is_symlink()
    except (ValueError, OSError):
        return False


def _x_ensure_dir(a, env, store, host, h, undo=False):
    d = Path(a["dir"])
    if d != env.home / OPENWEBUI_DIR:
        raise StepFailed("path_not_ours", "the folder is not Open WebUI's own folder")
    if undo:
        if d.exists():
            if d.is_symlink():
                raise StepFailed("path_not_ours", "the folder is a link: it was not deleted")
            shutil.rmtree(d)
        return {"deleted": True}
    for sub in ("", "data", "run"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    envrec_os.current().protect_private(d)
    return {"made": True}


def _x_uv_install(a, env, store, host, h, undo=False):
    venv = Path(a["venv"])
    if undo:
        if venv.exists() and _inside(venv, Path(a["dir"])):
            shutil.rmtree(venv)
        return {"deleted": True}
    for argv in a["argv"]:
        done = _run_cli(env, argv, 3600.0)
        if done.returncode != 0:
            raise StepFailed("install_failed", f"{Path(argv[0]).name} ended with code {done.returncode}")
    if not Path(a["venv_py"]).exists():
        raise StepFailed("install_failed", "the environment has no Python after the install")
    return {"installed": True}


def _http(port: int, method: str, path: str, body: dict | None = None, token: str | None = None, timeout: float = 30.0) -> tuple[int, Any]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    headers = {"Content-Type": "application/json"} if body is not None else {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        r = conn.getresponse()
        raw = r.read()
        try:
            return r.status, json.loads(raw) if raw else None
        except ValueError:
            return r.status, None
    finally:
        conn.close()


def _wait_ready(port: int, seconds: float, clock=time.time, sleep=time.sleep) -> bool:
    end = clock() + seconds
    while clock() < end:
        try:
            if _http(port, "GET", "/health", timeout=3.0)[0] == 200:
                return True
        except OSError:
            pass
        sleep(1.0)
    return False


def _x_owui_start(a, env, store, host, h, undo=False):
    run = Path(a["run"])
    info_file = run / "server.json"
    if undo:
        try:
            info = json.loads(info_file.read_text())
            procs.stop(int(info["pid"]), float(info["created"]))
        except (OSError, ValueError, KeyError):
            pass
        return {"stopped": True}
    secret = Path(a["secret"])
    fresh = secrets.token_urlsafe(18)
    secret.write_text(json.dumps({"email": ADMIN_EMAIL, "password": fresh}))
    envrec_os.current().protect_private(secret)
    penv = {**_owui_env(a), "WEBUI_ADMIN_EMAIL": ADMIN_EMAIL, "WEBUI_ADMIN_PASSWORD": fresh, "WEBUI_ADMIN_NAME": "Admin"}
    pid = envrec_os.current().spawn_detached([a["exe"], "serve", "--host", "127.0.0.1", "--port", str(a["port"])], Path(a["data"]), run / "server.log", penv)
    created = procs.created_at(pid)
    info_file.write_text(json.dumps({"pid": pid, "created": created, "port": a["port"]}))
    if not _wait_ready(a["port"], 300.0, env.clock):
        procs.stop(pid, created or 0.0)
        raise StepFailed("server_not_ready", "Open WebUI did not answer in time and was stopped")
    return {"started": True, "listening": f"127.0.0.1:{a['port']}", "admin_email": ADMIN_EMAIL}  # the password is in the 0600 file only: a chat, a log and a fact never see it


def _signin(a) -> str:
    cred = json.loads(Path(a["secret"]).read_text())
    status, doc = _http(a["port"], "POST", "/api/v1/auths/signin", {"email": cred["email"], "password": cred["password"]})
    if status != 200 or not isinstance(doc, dict) or not doc.get("token"):
        raise StepFailed("signin_refused", f"Open WebUI refused the admin sign-in (status {status})")
    return doc["token"]  # held in memory for this step only: never logged, shown or stored


def _x_owui_register(a, env, store, host, h, undo=False):
    token = _signin(a)
    base = f"/api/v1/functions/id/{FUNCTION_ID}"
    if undo:
        status, _ = _http(a["port"], "DELETE", f"{base}/delete", token=token)
        if status not in (200, 404):
            raise StepFailed("delete_refused", f"Open WebUI refused to delete the function (status {status})")
        return {"deleted": True}
    pipe = Path(a["pipe"])
    if sha_of(pipe) != a["pipe_sha256"]:
        raise StepFailed("pipe_changed", "the packaged Pipe is not the file this plan showed")
    status, _ = _http(a["port"], "POST", "/api/v1/functions/create", {"id": FUNCTION_ID, "name": "Multi Auto Setup", "content": pipe.read_text(), "meta": {"description": "The owner's private network of computers, from Open WebUI."}}, token)
    if status not in (200,):
        raise StepFailed("create_refused", f"Open WebUI refused to create the function (status {status})")
    status, doc = _http(a["port"], "GET", base, token=token)
    if status == 200 and isinstance(doc, dict) and not doc.get("is_active"):
        status, _ = _http(a["port"], "POST", f"{base}/toggle", {}, token)
        if status != 200:
            raise StepFailed("toggle_refused", f"Open WebUI refused to turn the function on (status {status})")
    status, _ = _http(a["port"], "POST", f"{base}/valves/update", {"STATE_DIR": a["state_dir"]}, token)
    if status != 200:
        raise StepFailed("valves_refused", f"Open WebUI refused the valve (status {status})")
    return {"registered": True}


def _x_owui_verify(a, env, store, host, h, undo=False):
    if undo:
        return {}
    token = _signin(a)
    base = f"/api/v1/functions/id/{FUNCTION_ID}"
    status, doc = _http(a["port"], "GET", base, token=token)
    if status != 200 or not isinstance(doc, dict) or not doc.get("is_active"):
        raise StepFailed("function_not_on", "the function is missing or off")
    status, valves = _http(a["port"], "GET", f"{base}/valves", token=token)
    if status != 200 or not isinstance(valves, dict) or valves.get("STATE_DIR") != a["state_dir"]:
        raise StepFailed("valve_not_set", "the valve STATE_DIR is not set as planned")
    return {"ok": True}


def _x_stop_legacy(a, env, store, host, h, undo=False):
    if undo:
        d = Path(a["dir"])
        if not (d / "data").exists():
            raise StepFailed("folder_missing", "the folder is not back in its place yet: undo the move first")
        pid = envrec_os.current().spawn_detached([a["exe"], "serve", "--host", "127.0.0.1", "--port", str(a["port"])], d / "data", d / "restart.log", _legacy_env(a["dir"]))
        if not _wait_ready(int(a["port"]), 300.0, env.clock):
            procs.stop(pid, procs.created_at(pid) or 0.0)
            raise StepFailed("server_not_ready", "Open WebUI did not answer in time after the restart and was stopped")
        return {"restarted": True}
    pid, created = int(a["pid"]), float(a["created"])
    if not procs.is_ours(pid, created):
        raise StepFailed("process_changed", "the server that was planned to stop is not running as planned: nothing was stopped")
    try:
        argv = psutil.Process(pid).cmdline()
    except psutil.Error as exc:
        raise StepFailed("process_changed", type(exc).__name__) from exc
    if a["exe"] not in argv:
        raise StepFailed("process_changed", "that process is not the Open WebUI server of the folder: nothing was stopped")
    procs.stop(pid, created)
    return {"stopped": True}


def _x_move_aside(a, env, store, host, h, undo=False):
    src, dst = Path(a["dir"]), Path(a["dst"])
    if src != env.home / LEGACY_DIR:
        raise StepFailed("path_not_ours", "the folder is not the manual Open WebUI install")
    if undo:
        if dst.exists() and not src.exists():
            dst.rename(src)
            return {"moved_back": True}
        if src.exists():
            return {"moved_back": False}
        raise StepFailed("cannot_restore", "the moved folder is not where it was put")
    if src.is_symlink() or not src.is_dir():
        raise StepFailed("path_not_ours", "the folder is missing or is a link: it was not moved")
    if dst.exists():
        raise StepFailed("destination_exists", "the new name is already taken: nothing was moved")
    src.rename(dst)
    return {"moved": True}


def _x_verify_gone(a, env, store, host, h, undo=False):
    if undo:
        return {}
    d = Path(a["dir"])
    if d.exists():
        raise StepFailed("still_there", "the folder is still in its place")
    if any(a["exe"] in p["argv"] and "serve" in p["argv"] for p in (env.processes or _system_processes)()):
        raise StepFailed("still_running", "a server started from the folder is still running")
    return {"ok": True}


_EXEC = {"stop_legacy_server": _x_stop_legacy, "move_legacy_aside": _x_move_aside, "verify_legacy_gone": _x_verify_gone, "backup_file": _x_backup, "json_merge_entry": _x_json_merge, "json_verify_entry": _x_json_verify, "toml_add_table": _x_toml_add, "toml_verify_table": _x_toml_verify, "cli_add": _x_cli_add,
         "cli_verify_listed": _x_cli_verify, "ensure_dir": _x_ensure_dir, "uv_install_pinned": _x_uv_install, "openwebui_start": _x_owui_start, "openwebui_register": _x_owui_register, "openwebui_verify": _x_owui_verify}


def run_step(plan: dict, n: int, action: str, env: Env, recipes: dict | None = None) -> dict:
    """Run (or undo) one step of a plan whose hash was checked and whose step the owner approved. Raises StepFailed with a typed code."""
    r = recipes or load_recipes()
    host = plan["option_id"]
    h = r["hosts"][host]
    step = plan["steps"][n - 1]
    fn = _EXEC.get(step["op"])
    if fn is None:
        raise StepFailed("unknown_op", step["op"])
    return fn(step["args"], env, Store(env.state_dir, host), host, h, undo=(action == "undo"))


def read_admin_account(env: Env) -> dict | None:
    """For the owner's own screen or terminal only (never a chat, a log or a fact): the Open WebUI admin account this setup made, from the file only the owner can read."""
    try:
        doc = json.loads((env.home / OPENWEBUI_DIR / "admin.secret").read_text())
        return {"email": doc["email"], "password": doc["password"]}
    except (OSError, ValueError, KeyError):
        return None

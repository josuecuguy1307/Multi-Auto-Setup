"""Parse other AI agents and assistants, running or installed, and their MCP servers."""
import os
import os
import re
import json
import ipaddress
from collections import Counter

from envrec_sbx import *  # noqa: F401,F403
from envrec_sbx import GB, GIB, Caps, Out, Lookups, by_name, json_of, lost, num, first_version, clean_text, mask_ids, run_parser

SYSTEM_PREFIXES = ("/sbin/", "/usr/sbin/", "/usr/libexec/", "/System/", "/Library/Apple/", "/usr/bin/", "/bin/", "/usr/lib/", "/lib/", "/lib64/", "/usr/lib64/", "/sbin", "/init")


ASSET = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "agents.json")))
CLIENTS = [m["client"] for m in ASSET.get("mcp_configs", [])]


def load_agents():
    return ASSET


def _parse(captures, platform):
    c = by_name(captures)
    spec = load_agents()
    ps = c.get("ps_comm")
    if ps is None or not ps.ok:
        return lost(ps, "app.agent_count")
    running = {os.path.basename(ln.strip()) for ln in ps.text.splitlines() if ln.strip()}
    wb = c.get("which_bins")
    bins = {os.path.basename(ln.strip()) for ln in wb.text.splitlines() if ln.startswith("/")} if wb and wb.text else set()
    apps = {ln.strip() for ln in c["ls_apps"].text.splitlines()} if c.get("ls_apps") and c["ls_apps"].ok else set()
    cfgs = {re.sub(r"^/(?:Users|home)/[^/]+/", "~/", ln.strip()) for ln in c["ls_cfg"].text.splitlines() if ln.startswith("/")} if c.get("ls_cfg") and c["ls_cfg"].text else set()
    out: list = []
    known_proc: set[str] = set()
    found = 0
    for a in spec.get("agents", []):
        known_proc |= set(a.get("processes", []))
        how = []
        if running & set(a.get("processes", [])):
            how.append("process")
        if bins & set(a.get("bins", [])):
            how.append("binary")
        if apps & set(a.get("apps", [])):
            how.append("app")
        if any(d in cfgs for d in a.get("config_dirs", [])):
            how.append("config")
        if not how:
            continue
        found += 1
        state = "running" if "process" in how else "installed"
        out.append(Entity("agent", 0, a["id"], "owned", {"name": a["name"], "category": a["category"], "state": state, "seen_as": how, "source": a["src"]},
                          prefix="app", src="ps_comm"))
    out.insert(0, FactOut("app.agent_count", found, src="ps_comm"))
    for cap in captures:
        if cap.name.startswith("mcp_") and cap.ok:
            client = CLIENTS[int(cap.name[4:])]
            for name, cmd in json.loads(cap.text).items():
                out.append(Entity("mcp_server", 0, f"{client}:{name}", "owned", {"name": name, "command": cmd, "client": client}, prefix="app", src=cap.name))
    # Generic: a running program that looks like an agent but is not in the table becomes an Unknown for the ladder.
    pats = [re.compile(re.escape(p), re.I) for p in spec.get("generic_patterns", [])]
    lst = c.get("lsof_tcp")
    listening = set(re.findall(r"^c(.+)$", lst.text, re.M)) | set(re.findall(r'users:\(\("([^"]+)"', lst.text)) if lst and lst.ok else set()
    user_proc = {os.path.basename(ln.strip()) for ln in ps.text.splitlines() if ln.strip() and not ln.strip().startswith(SYSTEM_PREFIXES)}
    for name in sorted((running - known_proc) & listening & user_proc):
        if any(p.search(name) for p in pats) and len(name) > 2:
            out.append(Entity("unknown_agent", 0, "proc:" + name, "unknown", {"name": name, "why": "a user program listening on a local port, name matches a generic agent pattern"}, prefix="app", resolved=False, src="ps_comm"))
    return out


def parse(inp):
    caps = Caps(inp)
    out = Out(caps)
    for i, m in enumerate(ASSET.get("mcp_configs", [])):
        cid = f"mcp_{i}"
        if cid not in caps:
            out.want(cid, "read_file", {"path": m["path"], "name": cid}, reducer="mcp_servers")
    if out.calls and caps.round == 0:
        return out.result()  # round 1: the MCP config files, reduced to {server name: command basename} before anything sees them
    return run_parser(inp, lambda captures, platform, caps: _parse(captures, platform))

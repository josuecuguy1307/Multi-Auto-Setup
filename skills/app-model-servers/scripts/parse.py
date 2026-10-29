"""Parse the local model servers found on listening ports."""
import os
import re
import json
import ipaddress
from collections import Counter

from envrec_sbx import *  # noqa: F401,F403
from envrec_sbx import GB, GIB, Caps, Out, Lookups, by_name, json_of, lost, num, first_version, clean_text, mask_ids, run_parser

SERVERS = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "model_servers.json")))["servers"]


def _ports(text: str) -> set[int]:
    ports = {int(m) for m in re.findall(r":(\d+)$", text, re.M)}
    ports |= {int(m) for m in re.findall(r":(\d+)\s+\S+:\*", text)}
    return ports

def _parse(captures, platform):
    c = by_name(captures)
    base = captures[0]
    if not base.ok:
        return lost(base, "app.model_server_count")
    servers = SERVERS
    listening = _ports(base.text)
    out: list = []
    seen: set[tuple[str, int]] = set()
    for s in servers:
        for port in s["ports"]:
            cap = c.get(f"http_{s['name']}_{port}".replace(".", "_"))
            if cap is None or not cap.ok or (s["name"], port) in seen:
                continue
            if re.search(s["match"], cap.text):
                if any(p == port and n != s["name"] and n != "openai-compatible" for n, p in seen):
                    continue
                seen.add((s["name"], port))
                ver = None
                try:
                    ver = json.loads(cap.text).get("version")
                except ValueError:
                    pass
                out.append(Entity("model_server", 0, f"{s['name']}:{port}", "owned", {"kind": s["name"], "port": port, "version": ver}, src=cap.name))
    return [FactOut("app.model_server_count", len(out), src=base.name)] + out


def parse(inp):
    caps = Caps(inp)
    base = caps.get("lsof_tcp") or caps.get("ss_tcp")
    if base is not None and base.ok:
        listening = _ports(base.text)
        out = Out(caps)
        for s in SERVERS:
            for port in s["ports"]:
                cid = f"http_{s['name']}_{port}".replace(".", "_")
                if port in listening and cid not in caps:
                    out.want(cid, "http_get", {"url": f"http://127.0.0.1:{port}{s['path']}"})
        if out.calls:
            return out.result()  # round 1: one read-only identify request to each listening port in the table
    return run_parser(inp, lambda captures, platform, caps: _parse(captures, platform))

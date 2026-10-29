"""Parse the listening TCP ports and the program that owns each."""
import os
import re
import json
import ipaddress
from collections import Counter

from envrec_sbx import *  # noqa: F401,F403
from envrec_sbx import GB, GIB, Caps, Out, Lookups, by_name, json_of, lost, num, first_version, clean_text, mask_ids, run_parser

def _parse(captures, platform):
    c = captures[0]
    if not c.ok:
        return lost(c, "net.listening.tcp")
    rows: dict[tuple[str, int], str] = {}
    if platform == "macos":
        proc = None
        for ln in c.text.splitlines():
            if ln.startswith("p"):
                proc = None
            elif ln.startswith("c"):
                proc = ln[1:]
            elif ln.startswith("n") and proc:
                m = re.match(r"n(.*):(\d+)$", ln)
                if m:
                    local = m.group(1) in ("127.0.0.1", "[::1]", "localhost")
                    rows[(proc, int(m.group(2)))] = "loopback" if local else "all"
    else:
        for ln in c.text.splitlines():
            m = re.search(r"(\S+):(\d+)\s+\S+:\*\s*(?:users:\(\(\"([^\"]+)\")?", ln)
            if m:
                rows[(m.group(3) or "unknown", int(m.group(2)))] = "loopback" if m.group(1) in ("127.0.0.1", "[::1]") else "all"
    items = [{"name": n, "port": p, "scope": s} for (n, p), s in sorted(rows.items(), key=lambda kv: kv[0][1])][:80]
    return [FactOut("net.listening.tcp", items, src=c.name, limitations=["program names only; command lines are never read",
                                                                            "other users' processes may be invisible without privileges"])]


def parse(inp):
    return run_parser(inp, lambda captures, platform, caps: _parse(captures, platform))

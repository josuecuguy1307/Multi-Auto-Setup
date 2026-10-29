"""Parse executables in user bin directories that the tools table does not cover."""
import os
import re
import json
import ipaddress
from collections import Counter

from envrec_sbx import *  # noqa: F401,F403
from envrec_sbx import GB, GIB, Caps, Out, Lookups, by_name, json_of, lost, num, first_version, clean_text, mask_ids, run_parser

TOOLS = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "tools.json")))["tools"]


def load_tools():
    return TOOLS


def _parse(captures, platform):
    known = {t["bin"] for t in load_tools()}
    names: set[str] = set()
    for c in captures:
        if c.ok:
            names |= {ln.strip() for ln in c.text.splitlines() if ln.strip() and not ln.startswith(".")}
    unknown = sorted(n for n in names if n not in known)[:40]
    if not any(c.ok for c in captures):
        return [UnknownOut("app.unknown_executable_count", Reason.not_applicable, "no user bin directory exists")]
    return [FactOut("app.unknown_executable_count", len(unknown), src=captures[0].name)] + [
        Entity("unknown_exe", 0, "exe:" + n, "owned", {"name": n}, resolved=False, src=next(c.name for c in captures if c.ok)) for n in unknown]


def parse(inp):
    return run_parser(inp, lambda captures, platform, caps: _parse(captures, platform))

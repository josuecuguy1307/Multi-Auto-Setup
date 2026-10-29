"""Parse the developer tools found, with versions."""
import os
import os
import re
import json
from collections import Counter
import ipaddress
from collections import Counter

from envrec_sbx import *  # noqa: F401,F403
from envrec_sbx import GB, GIB, Caps, Out, Lookups, by_name, json_of, lost, num, first_version, clean_text, mask_ids, run_parser

TOOLS = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "tools.json")))["tools"]


def load_tools():
    return TOOLS


def _parse(captures, platform):
    c = by_name(captures)
    w = c.get("which_all")
    tools = load_tools()
    if w is None or (w.error is not None and not w.text):
        return lost(w, "app.tools_found")
    found = {os.path.basename(ln.strip()) for ln in w.text.splitlines() if ln.startswith("/")}
    out: list = []
    by_cat: Counter = Counter()
    for t in tools:
        if t["bin"] not in found:
            continue
        cap = c.get(f"ver_{t['bin']}".replace(".", "_"))
        ver = first_version(cap.text) if cap is not None and cap.ok else None
        by_cat[t["category"]] += 1
        out.append(Entity("app", 0, "tool:" + t["bin"], "owned", {"name": t["bin"], "kind": "tool", "category": t["category"], "version": ver,
                                                                  "source": t["src"]}, prefix="app", src=cap.name if cap is not None and cap.ok else "which_all"))
    return [FactOut("app.tools_found", len(found & {t["bin"] for t in tools}), src="which_all"), FactOut("app.tools_checked", len(tools), src="which_all"),
            FactOut("app.tools_by_category", dict(by_cat), src="which_all")] + out


def parse(inp):
    caps = Caps(inp)
    w = caps.get("which_all")
    if w is not None and w.text:
        found = {os.path.basename(ln.strip()) for ln in w.text.splitlines() if ln.startswith("/")}
        out = Out(caps)
        for t in TOOLS:
            cid = f"ver_{t['bin']}".replace(".", "_")
            if t["bin"] in found and cid not in caps:
                out.want(cid, "run_command", {"argv": [t["bin"], *t["args"]], "timeout_s": 8, "optional": True, "merge_stderr": True})
        if out.calls:
            return out.result()  # round 1: ask for the version of only the tools that exist
    return run_parser(inp, lambda captures, platform, caps: _parse(captures, platform))

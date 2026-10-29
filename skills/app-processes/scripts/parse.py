"""Parse the running programs by executable name."""
import os
import re
import json
import ipaddress
from collections import Counter

from envrec_sbx import *  # noqa: F401,F403
from envrec_sbx import GB, GIB, Caps, Out, Lookups, by_name, json_of, lost, num, first_version, clean_text, mask_ids, run_parser

SYSTEM_PREFIXES = ("/sbin/", "/usr/sbin/", "/usr/libexec/", "/System/", "/Library/Apple/", "/usr/bin/", "/bin/", "/usr/lib/", "/lib/", "/lib64/", "/usr/lib64/", "/sbin", "/init")

def _parse(captures, platform):
    c = captures[0]
    if not c.ok:
        return lost(c, "app.process_count", "app.processes")
    counts: Counter = Counter()
    cls: dict[str, str] = {}
    for ln in c.text.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        name = os.path.basename(ln)
        counts[name] += 1
        cls.setdefault(name, "system" if ln.startswith(SYSTEM_PREFIXES) else "user")
    rows = [{"name": n, "count": k, "class": cls[n]} for n, k in counts.most_common(80)]
    by_cls = Counter()
    for n, k in counts.items():
        by_cls[cls[n]] += k
    return [FactOut("app.process_count", sum(counts.values()), src="ps_comm"), FactOut("app.process_classes", dict(by_cls), src="ps_comm"),
            FactOut("app.processes", rows, src="ps_comm", limitations=["executable names only: arguments and environment are never read"])]


def parse(inp):
    return run_parser(inp, lambda captures, platform, caps: _parse(captures, platform))

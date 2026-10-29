"""Parse the kernel sensors."""
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
        return lost(c, "sensor.count")
    chips, out = {}, []
    cur = None
    for ln in c.text.splitlines():
        if ln and not ln.startswith((" ", "Adapter:")) and not ln.endswith(":"):
            cur = ln.strip()
            chips[cur] = []
        elif cur and re.match(r"\s+(temp|in|fan|curr|power)\d+_input:", ln):
            chips[cur].append((ln.split(":")[0].strip(), num(ln.split(":")[1])))
    for chip, vals in chips.items():
        out.append(Entity("sensor", 0, chip, "owned", {"chip": chip, "channels": len(vals), "kinds": sorted({re.sub(r"\d+_input", "", k) for k, _ in vals})}, src="sensors"))
    return [FactOut("sensor.count", len(chips), src="sensors")] + out


def parse(inp):
    return run_parser(inp, lambda captures, platform, caps: _parse(captures, platform))

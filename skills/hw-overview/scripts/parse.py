"""Parse the machine model, chip and cores."""
import os
import re
import json
import ipaddress
from collections import Counter

from envrec_sbx import *  # noqa: F401,F403
from envrec_sbx import GB, GIB, Caps, Out, Lookups, by_name, json_of, lost, num, first_version, clean_text, mask_ids, run_parser

def _parse(captures, platform):
    c = by_name(captures)
    if platform == "macos":
        d = json_of(c.get("sp_hardware"))
        if not d:
            return lost(c.get("sp_hardware"), *('hw.machine_model', 'hw.machine_name', 'hw.chip'))
        h = d["SPHardwareDataType"][0]
        out = [FactOut("hw.machine_model", h.get("machine_model")), FactOut("hw.machine_name", h.get("machine_name")),
               FactOut("hw.chip", h.get("chip_type") or h.get("cpu_type"))]
        if h.get("number_processors"):
            out.append(FactOut("hw.core_layout", str(h["number_processors"]), limitations=["format 'proc total:performance:efficiency'"]))
        return [o for o in out if o.value]
    lscpu = c.get("lscpu")
    if lscpu is None or not lscpu.ok:
        return lost(lscpu, "hw.chip")
    kv = dict(re.findall(r"^([A-Za-z() -]+):\s+(.*)$", lscpu.text, re.M))
    out = [FactOut("hw.chip", kv.get("Model name") or kv.get("Architecture"))]
    dt = c.get("dt_model")
    if dt and dt.ok and dt.text.strip("\x00 \n"):
        out.append(FactOut("hw.machine_model", dt.text.strip("\x00 \n")))
    return [o for o in out if o.value]


def parse(inp):
    return run_parser(inp, lambda captures, platform, caps: _parse(captures, platform))

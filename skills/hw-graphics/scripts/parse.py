"""Parse the GPUs and attached displays."""
import os
import re
import json
import ipaddress
from collections import Counter

from envrec_sbx import *  # noqa: F401,F403
from envrec_sbx import GB, GIB, Caps, Out, Lookups, by_name, json_of, lost, num, first_version, clean_text, mask_ids, run_parser

def strip_prefix(value, prefix):
    return value[len(prefix):] if isinstance(value, str) and value.startswith(prefix) else value


def _parse(captures, platform):
    c = by_name(captures)
    if platform != "macos":
        p = c.get("lspci")
        if p is None or not p.ok:
            return lost(p, "hw.gpu_count")
        gpus = [ln.split(": ", 1)[-1] for ln in p.text.splitlines() if re.search(r"VGA|3D controller|Display controller", ln)]
        return [FactOut("hw.gpu_count", len(gpus))] + [Entity("gpu", 0, g, "owned", {"name": g}, src="lspci") for g in gpus]
    d = json_of(c.get("sp_displays"))
    if not d:
        return lost(c.get("sp_displays"), *('hw.gpu_count', 'hw.display_count'))
    out, gpus, displays = [], 0, 0
    for g in d["SPDisplaysDataType"]:
        gpus += 1
        out.append(Entity("gpu", 0, g.get("_name", "gpu") + str(g.get("sppci_bus", "")), "owned",
                          {"name": g.get("sppci_model") or g.get("_name"), "cores": g.get("sppci_cores"), "metal": strip_prefix(g.get("spdisplays_mtlgpufamilysupport"), "spdisplays_"),
                           "bus": strip_prefix(g.get("sppci_bus"), "spdisplays_")}, src="sp_displays"))
        for dsp in g.get("spdisplays_ndrvs", []):
            displays += 1
            out.append(Entity("display", 0, str(dsp.get("_spdisplays_displayID", displays)) + dsp.get("_name", ""), "owned",
                              {"name": dsp.get("_name"), "resolution": dsp.get("_spdisplays_resolution"),
                               "connection": strip_prefix(dsp.get("spdisplays_connection_type"), "spdisplays_"),
                               "main": dsp.get("spdisplays_main") == "spdisplays_yes"}, src="sp_displays"))
    return [FactOut("hw.gpu_count", gpus, src="sp_displays"), FactOut("hw.display_count", displays, src="sp_displays")] + out


def parse(inp):
    return run_parser(inp, lambda captures, platform, caps: _parse(captures, platform))

"""Parse the UPnP devices that answered."""
import os
import re
import json
import ipaddress
from collections import Counter

from envrec_sbx import *  # noqa: F401,F403
from envrec_sbx import GB, GIB, Caps, Out, Lookups, by_name, json_of, lost, num, first_version, clean_text, mask_ids, run_parser

RULES = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "lan_rules.json")))


def load_rules():
    return RULES


def _parse(captures, platform):
    c = captures[0]
    if not c.ok:
        return lost(c, "net.ssdp_devices")
    devs = json.loads(c.text)
    rules = load_rules().get("ssdp_types", {})
    out: list = [FactOut("net.ssdp_devices", len(devs), src=c.name)]
    for d in devs:
        kinds = [d["fields"].get("deviceType", "")] + d["st"]
        hint = next((r["hint"] for k, r in rules.items() if any(k in x for x in kinds)), None)
        attrs = {"ip": d["ip"], "server": d["server"], "device_type": d["fields"].get("deviceType"), "name": d["fields"].get("friendlyName"),
                 "manufacturer": d["fields"].get("manufacturer"), "model": d["fields"].get("modelName"), "meaning": hint}
        out.append(Entity("service", 2, f"ssdp|{d['ip']}", "unknown", attrs, prefix="net", resolved=bool(hint or attrs["manufacturer"]), src=c.name))
    return out


def parse(inp):
    return run_parser(inp, lambda captures, platform, caps: _parse(captures, platform))

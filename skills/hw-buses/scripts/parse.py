"""Parse Thunderbolt ports and PCI devices."""
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
        return lost(c, "hw.thunderbolt_ports" if platform == "macos" else "hw.pci_device_count")
    if platform == "macos":
        d = json_of(c)
        if not d:
            return lost(c, "hw.thunderbolt_ports")
        ports, devices = 0, []
        for bus in d.get("SPThunderboltDataType", []):
            for k, v in bus.items():
                if k.startswith("receptacle_") and isinstance(v, dict):
                    ports += 1
                    if v.get("receptacle_status_key", v.get("receptacle_status")) != "receptacle_no_devices_connected":
                        devices.append(f"{bus.get('_name')}:{k}")
        return [FactOut("hw.thunderbolt_ports", ports, src=c.name), FactOut("hw.thunderbolt_devices_connected", len(devices), src=c.name)] + [
            Entity("thunderbolt", 1, d_, "owned", {"port": d_.split(":")[1]}, src=c.name) for d_ in devices]
    rows = [re.match(r"(\S+) (.+?) \[([0-9a-f]{4})\]: (.+) \[([0-9a-f]{4}):([0-9a-f]{4})\]", ln) for ln in c.text.splitlines()]
    rows = [m for m in rows if m]
    return [FactOut("hw.pci_device_count", len(rows), src=c.name)] + [
        Entity("pci", 1, m.group(1), "owned", {"class": m.group(2), "name": m.group(4), "vid": m.group(5), "pid": m.group(6)}, src=c.name) for m in rows]


def parse(inp):
    return run_parser(inp, lambda captures, platform, caps: _parse(captures, platform))

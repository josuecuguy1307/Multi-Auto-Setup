"""Parse the serial port names."""
import os
import re
import json
import ipaddress
from collections import Counter

from envrec_sbx import *  # noqa: F401,F403
from envrec_sbx import GB, GIB, Caps, Out, Lookups, by_name, json_of, lost, num, first_version, clean_text, mask_ids, run_parser

def _parse(captures, platform):
    c = by_name(captures)
    dev = c.get("dev_list")
    if dev is None or not dev.ok:
        return lost(dev, "serial.port_count")
    cfg = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "serial_ports.json")))
    include, deny = cfg.get("include", []), cfg.get("deny", [])
    names = [n.strip() for n in dev.text.splitlines()]
    cand = [n for n in names if n.startswith(("cu.", "ttyUSB", "ttyACM", "ttyAMA", "ttyGS", "rfcomm"))]
    listed, excluded = [], 0
    for n in cand:
        if any(d in n for d in deny) or not any(i in n for i in include):
            excluded += 1
            continue
        listed.append(n)
    byid = c.get("by_id")
    ids = [ln.strip() for ln in byid.text.splitlines()] if byid and byid.ok else []
    out: list = [FactOut("serial.port_count", len(listed), src="dev_list"), FactOut("serial.excluded_count", excluded, src="dev_list",
                                                                                   limitations=["entries that are not serial devices (Bluetooth audio ports, debug consoles) are counted and never opened"])]
    for n in listed:
        tail = n.split("usbserial-")[-1].split("usbmodem")[-1] if ("usbserial-" in n or "usbmodem" in n) else None
        link_hint = next((i for i in ids if tail and tail in i), None)
        out.append(Entity("serial", 1, "/dev/" + n, "owned", {"name": n, "kind": "usb-serial" if ("usb" in n.lower() or "ACM" in n) else "uart", "by_id": link_hint},
                          links=[("usb", tail)] if tail else [], src="dev_list"))
    return out


def parse(inp):
    return run_parser(inp, lambda captures, platform, caps: _parse(captures, platform))

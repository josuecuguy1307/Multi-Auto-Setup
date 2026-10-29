"""Parse the neighbor table into host entities."""
import os
import re
import json
import ipaddress
from collections import Counter

from envrec_sbx import *  # noqa: F401,F403
from envrec_sbx import GB, GIB, Caps, Out, Lookups, by_name, json_of, lost, num, first_version, clean_text, mask_ids, run_parser

LK = None


def _lk(cid, tool, args):
    """A lookup answer: the tool's data, {} when its table is missing, None while it is still being fetched."""
    d = LK.call(cid, tool, args)
    return None if d is None else ({} if d.get("_failed") else d)


def _oui(mac):
    d = _lk("oui_" + mac.replace(":", "")[:6], "lookup_oui", {"mac_prefix": mac})
    return (d or {}).get("name")


def neighbor_entities(rows, src):
    out = []
    for r in rows:
        vendor = _oui(r["mac"])
        out.append(Entity("host", 2, r["mac"], "unknown", {"ip": r["ip"], "vendor": vendor, "oui": r["mac"].replace(":", "")[:6], "randomized_mac": _randomized(r["mac"]),
                                                           "interface": r["iface"]}, prefix="net", resolved=bool(vendor), src=src))
    return out


def _randomized(mac: str) -> bool:
    try:
        return bool(int(mac.split(":")[0], 16) & 0b10)
    except ValueError:
        return False

def parse_neighbors(text: str, platform: str) -> tuple[list[dict], int]:
    rows, incomplete = [], 0
    for ln in text.splitlines():
        if re.search(r"\bpermanent\b|\bPERMANENT\b", ln):
            continue  # this machine's own address, not a neighbor
        if platform == "macos":
            m = re.match(r"\S+ \((\d+\.\d+\.\d+\.\d+)\) at (\S+) on (\S+)", ln)
            if not m:
                continue
            ip, mac, iface = m.groups()
            if mac == "(incomplete)":
                incomplete += 1
                continue
        else:
            m = re.match(r"(\d+\.\d+\.\d+\.\d+) dev (\S+) lladdr (\S+)", ln)
            if not m:
                if re.match(r"\d+\.\d+\.\d+\.\d+ dev \S+ .*(FAILED|INCOMPLETE)", ln):
                    incomplete += 1
                continue
            ip, iface, mac = m.groups()
        mac = ":".join(p.zfill(2) for p in mac.lower().split(":"))
        try:
            addr = ipaddress.IPv4Address(ip)
        except ValueError:
            continue
        if addr.is_multicast or mac == "ff:ff:ff:ff:ff:ff" or ip.endswith(".255"):
            continue
        rows.append({"ip": ip, "mac": mac, "iface": iface})
    return rows, incomplete

def _parse(captures, platform):
    c = captures[0]
    if not c.ok:
        return lost(c, "net.neighbors_count")
    rows, inc = parse_neighbors(c.text, platform)
    return [FactOut("net.neighbors_count", len(rows), src=c.name, limitations=["only hosts already in the table; the table forgets idle hosts"]),
            FactOut("net.neighbors_incomplete", inc, src=c.name)] + neighbor_entities(rows, c.name)


def _run(captures, platform, caps):
    global LK
    LK = Lookups(caps)
    return _parse(captures, platform)


def parse(inp):
    return run_parser(inp, _run)

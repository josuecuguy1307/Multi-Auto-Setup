"""Parse the ping sweep and the neighbor table that follows it."""
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
    c = by_name(captures)
    sw = c.get("sweep")
    if sw is None or not sw.ok:
        return lost(sw, "net.lan_hosts_count")
    info = json.loads(sw.text)
    arp = next((x for x in captures if x.name in ("arp", "ip_neigh") and x.ok), None)
    rows = parse_neighbors(arp.text, platform)[0] if arp else []
    out: list = [FactOut("net.lan_hosts_count", len(info["alive"]), src="sweep", limitations=[f"{info['scanned']} addresses pinged once; hosts that drop ICMP stay unseen"]),
                 FactOut("net.lan_sweep_scope", {"network": info["network"], "scanned": info["scanned"], "rate_per_s": info["rate_per_s"]}, src="sweep")]
    if arp:
        out += neighbor_entities(rows, arp.name)
    return out


def _run(captures, platform, caps):
    global LK
    LK = Lookups(caps)
    return _parse(captures, platform)


def parse(inp):
    return run_parser(inp, _run)

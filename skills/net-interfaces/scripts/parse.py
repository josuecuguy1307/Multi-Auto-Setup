"""Parse own interfaces, the default route and DNS servers."""
import os
import re
import json
import ipaddress
from collections import Counter

from envrec_sbx import *  # noqa: F401,F403
from envrec_sbx import GB, GIB, Caps, Out, Lookups, by_name, json_of, lost, num, first_version, clean_text, mask_ids, run_parser

def _parse(captures, platform):
    c = by_name(captures)
    out: list = []
    addrs: list[dict] = []
    if platform == "macos":
        ifc = c.get("ifconfig")
        if ifc and ifc.ok:
            cur = None
            for ln in ifc.text.splitlines():
                m = re.match(r"^(\w+):", ln)
                if m:
                    cur = m.group(1)
                    continue
                m = re.match(r"\s+inet (\d+\.\d+\.\d+\.\d+) netmask (0x[0-9a-f]+)", ln)
                if m and cur:
                    prefix = bin(int(m.group(2), 16)).count("1")
                    addrs.append({"if": cur, "addr": f"{m.group(1)}/{prefix}"})
        else:
            out += lost(ifc, "net.addresses")
        rt = c.get("routes")
        gw = None
        if rt and rt.ok:
            for ln in rt.text.splitlines():
                p = ln.split()
                if len(p) >= 4 and p[0] == "default" and re.match(r"\d+\.\d+\.\d+\.\d+$", p[1]):
                    gw = {"gateway": p[1], "if": p[3] if len(p) > 3 else None}
                    break
        dns_servers = []
        d = c.get("dns")
        if d and d.ok:
            dns_servers = list(dict.fromkeys(re.findall(r"nameserver\[\d+\] : (\S+)", d.text)))
    else:
        ia = c.get("ip_addr")
        if ia and ia.ok:
            for ln in ia.text.splitlines():
                m = re.match(r"\d+:\s+(\S+)\s+inet (\S+)", ln)
                if m:
                    addrs.append({"if": m.group(1), "addr": m.group(2)})
        else:
            out += lost(ia, "net.addresses")
        gw = None
        r = c.get("ip_route")
        if r and r.ok:
            m = re.search(r"^default via (\S+) dev (\S+)", r.text, re.M)
            gw = {"gateway": m.group(1), "if": m.group(2)} if m else None
        rv = c.get("resolv")
        dns_servers = re.findall(r"^nameserver\s+(\S+)", rv.text, re.M) if rv and rv.ok else []
    usable = []
    for a in addrs:
        try:
            ip = ipaddress.ip_interface(a["addr"])
        except ValueError:
            continue
        if not (ip.ip.is_loopback or ip.ip.is_link_local):
            usable.append(a)
    if addrs or not any(isinstance(o, UnknownOut) for o in out):
        out.append(FactOut("net.addresses", [f"{a['if']} {a['addr']}" for a in usable], src="ifconfig" if platform == "macos" else "ip_addr"))
        for a in usable:
            out.append(Entity("host", 0, "self-" + a["if"], "owned", {"interface": a["if"], "address": a["addr"], "self": True}, prefix="net", src="ifconfig" if platform == "macos" else "ip_addr"))
    if gw:
        out += [FactOut("net.default_gateway", gw["gateway"], src="routes" if platform == "macos" else "ip_route"),
                FactOut("net.default_interface", gw["if"], src="routes" if platform == "macos" else "ip_route")]
    dns_cap = c.get("dns" if platform == "macos" else "resolv")
    if dns_cap is not None and dns_cap.ok:
        out.append(FactOut("net.dns_servers", dns_servers, src=dns_cap.name))
    else:
        out += lost(dns_cap, "net.dns_servers")
    return out


def parse(inp):
    return run_parser(inp, lambda captures, platform, caps: _parse(captures, platform))

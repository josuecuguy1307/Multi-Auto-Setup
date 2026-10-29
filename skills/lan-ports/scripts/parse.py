"""Parse the open ports and their banners."""
import os
import re
import json
import ipaddress
from collections import Counter

from envrec_sbx import *  # noqa: F401,F403
from envrec_sbx import GB, GIB, Caps, Out, Lookups, by_name, json_of, lost, num, first_version, clean_text, mask_ids, run_parser

PORTS = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "ports.json")))["ports"]


def load_ports():
    return PORTS


def _parse(captures, platform):
    c = captures[0]
    if not c.ok:
        return lost(c, "net.open_ports")
    d = json.loads(c.text)
    names = {p["port"]: p["name"] for p in load_ports()}
    out: list = [FactOut("net.open_ports", len(d["open"]), src=c.name, limitations=[f"{len(d['hosts'])} hosts x {len(d['ports'])} ports, connect only"])]
    for r in d["open"]:
        known = names.get(r["port"])
        attrs = {"ip": r["ip"], "port": r["port"], "service": known, "banner": r.get("banner"), "http_status": r.get("status"), "http_server": r.get("server"), "tls_subject": r.get("tls_subject")}
        resolved = bool(known and known != "unregistered") and (known not in ("http-alt", "unregistered") or bool(r.get("server") or r.get("banner")))
        out.append(Entity("service", 2, f"tcp|{r['ip']}|{r['port']}", "unknown", attrs, prefix="net", resolved=resolved, src=c.name))
    return out


def parse(inp):
    return run_parser(inp, lambda captures, platform, caps: _parse(captures, platform))

"""Parse mDNS service announcements, in rounds: service types, then instances, then where each one lives, then its addresses."""
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


RULES = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "lan_rules.json")))
TXT_KEYS = ("model", "md", "ty", "product", "manufacturer", "mfg", "am")
_SAFE_NAME = re.compile(r"^[^\x00-\x1f\-][^\x00-\x1f]{0,100}$")


def _safe(n):
    return bool(_SAFE_NAME.match(n))


def _instances(caps, types):
    """Instances found by the browse_i reads, in a fixed order (at most 10 per type and 60 in all)."""
    out = []
    for i in range(len(types)):
        b = caps.get(f"browse_{i}")
        if b is None:
            continue
        for t, n in parse_dnssd_instances(b.text)[:10]:
            if _safe(n):
                out.append((t, n))
    return out[:60]


def _hosts(caps, instances):
    hosts = []
    for j in range(len(instances)):
        r = caps.get(f"resolve_{j}")
        info = parse_dnssd_resolve(r.text) if r is not None else None
        if info and info["host"] not in hosts and _safe(info["host"]):
            hosts.append(info["host"])
    return hosts[:60]


def parse_dnssd_types(text: str) -> list[str]:
    out = []
    for ln in text.splitlines():
        m = re.search(r"\sAdd\s+\d+\s+\d+\s+\.\s+(_tcp|_udp)\.local\.\s+(_[\w-]+)\s*$", ln)
        if m:
            t = f"{m.group(2)}.{m.group(1)}"
            if t not in out:
                out.append(t)
    return out

def parse_dnssd_instances(text: str) -> list[tuple[str, str]]:
    out = []
    for ln in text.splitlines():
        m = re.search(r"\sAdd\s+\d+\s+\d+\s+local\.\s+(_[\w-]+\._(?:tcp|udp))\.\s+(.+?)\s*$", ln)
        if m and (m.group(1), m.group(2)) not in out:
            out.append((m.group(1), m.group(2)))
    return out

def parse_dnssd_resolve(text: str) -> dict | None:
    m = re.search(r"can be reached at (\S+?):(\d+)", text)
    if not m:
        return None
    txt = {k.lower(): v for k, v in re.findall(r"(\w+)=([^ ]*)", "\n".join(ln for ln in text.splitlines() if "=" in ln))}
    return {"host": m.group(1).rstrip("."), "port": int(m.group(2)), "txt": {k: v for k, v in txt.items() if k in TXT_KEYS}}

def parse_dnssd_addr(text: str) -> list[str]:
    return [a for a in dict.fromkeys(re.findall(r"\sAdd\s+\d+\s+\d+\s+\S+\s+(\d+\.\d+\.\d+\.\d+)\s+\d+", text)) if not a.startswith("127.")]


def _service_entities(types, services, src):
    out = [FactOut("net.mdns_service_types", len(types), src=src, limitations=["names of announced services only; TXT records are reduced to model fields"]),
           FactOut("net.mdns_services", len(services), src=src)]
    for s in services:
        t = s["type"]
        hint = (RULES.get("mdns_types", {}).get(t) or {}).get("hint")
        d = _lk("iana_" + re.sub(r"[^a-z0-9]+", "_", t.lower()), "lookup_service_type", {"service_type": t})
        desc = (d or {}).get("name")
        attrs = {"service_type": t, "instance": mask_ids(s["instance"]), "host": mask_ids(s["host"]), "port": s["port"], "ip": (s["ips"] or [None])[0], "meaning": desc or hint,
                 **{f"txt_{k}": v for k, v in s["txt"].items()}}
        out.append(Entity("service", 2, f"mdns|{t}|{s['instance']}|{s['host']}", "unknown", attrs, prefix="net", resolved=bool(desc or hint), src=src))
    return out


def _parse(captures, platform, caps, out):
    z = caps.get("zeroconf")
    if z is not None and z.ok:
        data = json.loads(z.text)
        services = [{"type": s["type"], "instance": s["instance"], "host": s["host"], "port": s["port"], "ips": s["ips"], "txt": s["txt"]} for s in data["services"]]
        return _service_entities(data["types"], services, "zeroconf")
    first = caps.get("types")
    if first is None or (first.error is not None and not first.text):
        return lost(first or z, "net.mdns_service_types")
    types = parse_dnssd_types(first.text)[:40]
    for t in types:  # what each service type means is asked together with the browse reads: one round less
        _lk("iana_" + re.sub(r"[^a-z0-9]+", "_", t.lower()), "lookup_service_type", {"service_type": t})
    for i, t in enumerate(types):
        if f"browse_{i}" not in caps:
            out.want(f"browse_{i}", "run_command", {"argv": ["dns-sd", "-B", t, "local"], "timeout_s": 10, "optional": True, "window_s": 2.0})
    if out.calls or caps.pending:
        return []
    instances = _instances(caps, types)
    for j, (t, n) in enumerate(instances):
        if f"resolve_{j}" not in caps:
            out.want(f"resolve_{j}", "run_command", {"argv": ["dns-sd", "-L", n, t, "local"], "timeout_s": 10, "optional": True, "window_s": 2.0}, reducer="txt_whitelist")
    if out.calls:
        return []
    hosts = _hosts(caps, instances)
    for k, h in enumerate(hosts):
        if f"addr_{k}" not in caps:
            out.want(f"addr_{k}", "run_command", {"argv": ["dns-sd", "-G", "v4", h], "timeout_s": 10, "optional": True, "window_s": 1.5})
    if out.calls:
        return []
    addr = {h: parse_dnssd_addr(caps.get(f"addr_{k}").text) for k, h in enumerate(hosts) if caps.get(f"addr_{k}") is not None}
    services = []
    for j, (t, n) in enumerate(instances):
        r = caps.get(f"resolve_{j}")
        info = parse_dnssd_resolve(r.text) if r is not None else None
        if info:
            services.append({"type": t, "instance": n, "host": info["host"], "port": info["port"], "ips": addr.get(info["host"], []), "txt": info["txt"]})
    return _service_entities(types, services, "types")


def parse(inp):
    caps = Caps(inp)
    out = Out(caps)
    global LK
    LK = Lookups(caps)
    outs = _parse(list(caps._c.values()), caps.platform, caps, out)
    if out.calls or caps.pending:
        for cid, tool, args in caps.pending:
            out.want(cid, tool, args)
        return {"facts": [], "entities": [], "unknowns": [], "next": out.calls}
    return to_result(outs, caps)

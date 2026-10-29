"""Device-class HYPOTHESES for hosts found on the local network, from the rules in data/lan_rules.yaml.

Signals per IP address: the OUI vendor, the mDNS service types, the SSDP device types, the open ports and being the default gateway. The result is
written as a hypothesis fact `net.device.<id>` listing the class candidates and every signal that caused them. It is never a fact and never `verified`."""

from __future__ import annotations

from collections import defaultdict

from envrec.contracts import CreatedBy, Evidence, Fact, FactStatus
from envrec.tools.builtin.lan import load_rules
from envrec.discovery.types import short_hash
from envrec.store import Store

CLASSES = ("router", "printer", "camera", "nas", "tv", "speaker", "phone", "sbc", "computer", "appliance")


def device_key(ip: str) -> str:
    return f"net.device.lan-{short_hash(ip)}"


def classify_signals(vendor: str | None, mdns: list[str], ssdp: list[str], ports: list[int], is_gateway: bool, rules: dict) -> tuple[list[str], list[str]]:
    """(class candidates best first, signals). Pure: tested without a store."""
    score: dict[str, float] = defaultdict(float)
    signals: list[str] = []
    if is_gateway:
        score["router"] += 3
        signals.append("is the default gateway")
    if vendor:
        for sub, classes in (rules.get("vendors") or {}).items():
            if sub in vendor.lower():
                signals.append(f"vendor {vendor!r}")
                for c in classes:
                    score[c] += 0.5
                break
    for t in mdns:
        r = (rules.get("mdns_types") or {}).get(t)
        if r and r.get("class"):
            signals.append(f"mDNS {t}")
            for c in r["class"]:
                score[c] += 1.5
    for t in ssdp:
        for k, r in (rules.get("ssdp_types") or {}).items():
            if r.get("class") and k in t:
                signals.append(f"SSDP {k}")
                for c in r["class"]:
                    score[c] += 1.5
    for p in ports:
        cls = (rules.get("ports") or {}).get(p)
        if cls:
            signals.append(f"open port {p}")
            for c in cls:
                score[c] += 0.75
    ranked = sorted(score, key=lambda c: (-score[c], c))
    top = [c for c in ranked if score[c] >= max(score.values(), default=0) - 0.01]
    return top, signals


def classify_lan(store: Store, target_id: str) -> int:
    """Write one hypothesis per IP seen on the LAN. Returns how many were written."""
    rules = load_rules()
    facts = store.current_facts(target_id)
    gw = next((f.value for f in facts if f.key == "net.default_gateway"), None)
    per_ip: dict[str, dict] = defaultdict(lambda: {"vendor": None, "mdns": [], "ssdp": [], "ports": []})
    for f in facts:
        if ".entity." not in f.key or f.status == FactStatus.unknown or not isinstance(f.value, dict) or f.value.get("ring") != 2:
            continue
        a = f.value.get("attrs", {})
        ip = a.get("ip")
        if not ip:
            continue
        rec = per_ip[ip]
        if f.value["kind"] == "host":
            rec["vendor"] = a.get("vendor") or rec["vendor"]
        elif a.get("service_type"):
            rec["mdns"].append(a["service_type"])
        elif a.get("device_type") or a.get("server") is not None and "port" not in a:
            rec["ssdp"] += [x for x in (a.get("device_type"), a.get("meaning")) if x]
        elif a.get("port"):
            rec["ports"].append(a["port"])
    n = 0
    for ip, rec in sorted(per_ip.items()):
        top, signals = classify_signals(rec["vendor"], rec["mdns"], rec["ssdp"], rec["ports"], ip == gw, rules)
        value = {"ip": ip, "class": top[0] if len(top) == 1 else None, "candidates": top[:4], "signals": signals[:8], "vendor": rec["vendor"]}
        ev = Evidence(provider="lan_rules", probe="classify", confidence=0.5 if top else 0.2,
                      limitations=["a class is a hypothesis from rules in data/lan_rules.yaml, never a fact"], note="derived from entities already in the store")
        store.write_fact(Fact(target_id=target_id, key=device_key(ip), value=value, status=FactStatus.hypothesis, evidence=[ev], created_by=CreatedBy.loop))
        n += 1
    return n

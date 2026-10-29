"""Parse nearby Bluetooth LE advertisers into vendors and service classes."""
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


def _name(prefix, tool, argname, value):
    d = _lk(prefix + re.sub(r"[^a-z0-9]+", "_", str(value).lower()), tool, {argname: str(value)})
    return (d or {}).get("name")


def _parse(captures, platform):
    c = captures[0]
    if not c.ok:
        return lost(c, "ble.nearby_count")
    try:
        recs = json.loads(c.text)
    except ValueError:
        return lost(c, "ble.nearby_count")
    out: list = []
    by_class: dict[str, int] = {}
    for r in recs:
        vendor = next((n for n in (_name('btc_', 'lookup_bluetooth_company', 'company_id', hex(x)) for x in r.get("company", [])) if n), None)
        svc = [n for n in (_name('bts_', 'lookup_bluetooth_service', 'uuid', u) for u in r.get("services", [])) if n]
        cls = vendor or (svc[0] if svc else "unclassified")
        by_class[cls] = by_class.get(cls, 0) + 1
        rssi = r.get("rssi")
        out.append(Entity("ble", 1, "adv:" + r["id"], "ambient", {"vendor": vendor, "service_classes": svc[:5], "signal": ("near" if rssi is not None and rssi > -60 else "far")},
                          resolved=bool(vendor or svc), src="scan", confidence=0.7))
    lim = ["counts and classes only: identifiers are hashed with a per-run salt and advertised names are dropped"]
    return [FactOut("ble.nearby_count", len(recs), src="scan", limitations=lim), FactOut("ble.nearby_by_class", dict(sorted(by_class.items(), key=lambda kv: -kv[1])[:20]), src="scan", limitations=lim)] + out


def _run(captures, platform, caps):
    global LK
    LK = Lookups(caps)
    return _parse(captures, platform)


def parse(inp):
    return run_parser(inp, _run)

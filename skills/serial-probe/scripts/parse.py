"""Parse the exchange with one serial port and match the reply against the protocol signatures."""
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


PLAN = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "serial_probes.json")))


def _parse(captures, platform):
    c = captures[0] if captures else None
    if c is None or not c.ok:
        return lost(c, "serial.probe_result")
    log = json.loads(c.text)
    text = (log.get("passive") or log.get("reply") or "")
    sent = log["sent"][-1] if log.get("sent") else None
    sent_raw = next((x["send"] for x in PLAN.get("probes", []) if x["name"] == sent), None)
    matches = []
    if text:
        d = _lk("serial_sig", "match_serial_signature", {"text": text, "sent": sent_raw.strip() if sent_raw else ""})
        matches = (d or {}).get("matches", [])
    attrs = {"port": log["port"].rsplit("/", 1)[-1], "baud": log.get("baud"), "responded": bool(text), "heard_unprompted": bool(log.get("passive")),
             "sent": log.get("sent"), "banner": text[:120], "protocol_guess": matches[0]["protocol"] if matches else None}
    return [Entity("serial_probe", 1, log["port"], "owned", attrs, links=[("serial", log["port"])], prefix="serial", resolved=bool(matches),
                   hypothesis=bool(matches), confidence=matches[0]["confidence"] if matches else 0.4, src=c.name)]


def _run(captures, platform, caps):
    global LK
    LK = Lookups(caps)
    return _parse(captures, platform)


def parse(inp):
    return run_parser(inp, _run)

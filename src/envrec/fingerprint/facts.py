"""Turn list lookups into hypothesis Facts. A list match is a guess: it never exceeds `hypothesis`.

The vendor and the model are separate facts. If only the vendor matched, the model stays unknown.
"""

from __future__ import annotations

from envrec.contracts import (
    Coverage,
    CreatedBy,
    Evidence,
    Fact,
    FactStatus,
    ProbeResult,
    Target,
    Unknown,
)
from envrec.fingerprint.base import Candidate
from envrec.fingerprint.serial_signatures import match_serial
from envrec.store import Store

LIST_LIMITS = ["a list match is a guess from an identifier, not a confirmed identification"]


def _hypothesis(store: Store, target: Target, key: str, value, cand: Candidate, probe: str,
                confidence: float, extra_limits: list[str], event_id: str | None) -> Fact:
    raw = store.put_raw(f"{cand.source} ({cand.data_version}): {cand.entry}")
    return Fact(
        target_id=target.id, key=key, value=value, status=FactStatus.hypothesis,
        evidence=[Evidence(provider=cand.source, probe=probe, event_id=event_id, raw_ref=raw,
                           confidence=confidence, limitations=[*LIST_LIMITS, *extra_limits],
                           note=cand.entry[:200])],
        created_by=CreatedBy.probe,
    )


def _result(facts: list[Fact], unknowns: list[Unknown], scope: str) -> ProbeResult:
    return ProbeResult(
        ok=True, facts=facts, unknowns=unknowns,
        coverage=Coverage(scope=scope, status="complete" if not unknowns else "partial",
                          checked=[f.key for f in facts if f.status != FactStatus.unknown],
                          unknown=[u.key for u in unknowns]),
    )


def _unknown_fact(target: Target, key: str) -> Fact:
    return Fact(target_id=target.id, key=key, created_by=CreatedBy.probe)


def usb_lookup_result(store: Store, target: Target, loader, vid: str, pid: str | None = None,
                      event_id: str | None = None) -> ProbeResult:
    cands = loader.lookup(f"{vid}:{pid}" if pid else vid)
    vendor = next((c for c in cands if c.level == "vendor"), None)
    product = next((c for c in cands if c.level == "product"), None)
    facts: list[Fact] = []
    unknowns: list[Unknown] = []
    if vendor:
        name = vendor.entry.split("  ", 1)[1]
        facts.append(_hypothesis(store, target, "usb.vendor_name", name, vendor, "lookup_usb_ids", 0.6,
                                 ["a vendor match identifies the vendor, not the model"], event_id))
    else:
        facts.append(_unknown_fact(target, "usb.vendor_name"))
        unknowns.append(Unknown(key="usb.vendor_name", question="vendor id is not in usb.ids"))
    if product:
        pname = product.entry.split(" / ", 1)[1]
        facts.append(_hypothesis(store, target, "usb.product_name", pname, product, "lookup_usb_ids", 0.6, [], event_id))
    else:
        facts.append(_unknown_fact(target, "usb.product_name"))
        why = "only the vendor matched; the model is not in usb.ids" if vendor else "vendor id is not in usb.ids"
        unknowns.append(Unknown(key="usb.product_name", question=why))
    return _result(facts, unknowns, "lookup_usb_ids")


def single_lookup_result(store: Store, target: Target, key: str, candidates: list[Candidate],
                         probe: str, event_id: str | None = None) -> ProbeResult:
    """One key from the first candidate (company id, OUI prefix, service type)."""
    if not candidates:
        return _result([_unknown_fact(target, key)], [Unknown(key=key, question="not in the list")], probe)
    cand = candidates[0]
    name = cand.entry.split("  ", 1)[1] if "  " in cand.entry else cand.entry
    return _result([_hypothesis(store, target, key, name, cand, probe, 0.5, [], event_id)], [], probe)


def serial_protocol_result(store: Store, target: Target, sample: bytes | str, sent: str | None = None,
                           event_id: str | None = None) -> ProbeResult:
    matches = match_serial(sample, sent)
    if not matches:
        return _result([_unknown_fact(target, "serial.protocol")],
                       [Unknown(key="serial.protocol", question="no known protocol signature matched")],
                       "match_serial_protocol")
    best = matches[0]
    fact = _hypothesis(store, target, "serial.protocol", best.protocol, best.as_candidate(),
                       "match_serial_protocol", best.confidence,
                       [f"also matched: {[m.protocol for m in matches[1:]]}"] if len(matches) > 1 else [], event_id)
    return _result([fact], [], "match_serial_protocol")

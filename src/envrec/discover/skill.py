"""A discovery result saved as a skill, keyed by the device signature (arch, OS, RAM bracket, accelerator).

A saved result is a PRIOR for any device with the same signature, never a fact about a new one: on another device the skill
is `unverified_here` until the recorded pair has been deployed and passed the real-model verifier THERE. A device with a
different signature gets no skill at all and goes through the rule table and discovery again.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from envrec.contracts import Actor, Event
from envrec.pairing.profile import DeviceProfile
from envrec.store import Store

VERIFIED_HERE, UNVERIFIED_HERE = "verified_here", "unverified_here"


def save_skill(store: Store, p: DeviceProfile, plan, result) -> str:
    winner = next(o for o in result.outcomes if o.entry_id == result.winner and o.status == "verified")
    raw = store.put_raw(json.dumps({"outcomes": [o.__dict__ for o in result.outcomes], "plan": plan.digest}, default=str))
    ev = store.append_event(Event(actor=Actor.verifier, type="discovery_skill", payload={
        "signature": p.signature, "entry": result.winner, "tokens_per_s": winner.tokens_per_s, "rss_mib": winner.rss_mib,
        "startup_s": winner.startup_s, "measured_on": p.target_id, "plan": plan.digest, "trials": len(result.outcomes),
        "raw": raw.sha256 if hasattr(raw, "sha256") else str(raw)}))
    return ev.id


def find_skill(store: Store, p: DeviceProfile) -> dict | None:
    """The newest saved result whose signature equals this device's, with its event id."""
    hit = None
    for e in store.events(type="discovery_skill"):
        if e.payload.get("signature") == p.signature:
            hit = {**e.payload, "id": e.id}
    return hit


def skill_status_here(store: Store, skill: dict, target_id: str) -> str:
    if skill.get("measured_on") == target_id:
        return VERIFIED_HERE  # measured on this very device
    for e in store.events(type="discovery_skill_verified_here"):
        if e.payload.get("skill") == skill["id"] and e.payload.get("target") == target_id:
            return VERIFIED_HERE
    return UNVERIFIED_HERE


def reverify_here(store: Store, skill: dict, p: DeviceProfile, deploy_and_verify: Callable[[str], object]) -> tuple[bool, str]:
    """Deploy the recorded pair on THIS device and run the real-model verifier. Marks the skill verified here only on a pass."""
    if find_skill(store, p) is None or find_skill(store, p)["id"] != skill["id"]:
        return False, "this skill's signature does not match this device"
    v = deploy_and_verify(skill["entry"])
    if not getattr(v, "ok", False):
        return False, f"the recorded pair did not verify here: {getattr(v, 'reason', 'no result')}"
    store.append_event(Event(actor=Actor.verifier, type="discovery_skill_verified_here", payload={
        "skill": skill["id"], "target": p.target_id, "tokens_per_s": getattr(v, "tokens_per_s", None)}))
    return True, "the recorded pair verified on this device"

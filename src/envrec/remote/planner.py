"""Connection planner: pick the best transport for ONE named target, from that target's allowed list only.

Inputs are the target's own config entry and read-only facts. It never connects, never tries a key, a
port or a transport that the config does not name, and never looks for another way in. If nothing listed
works it stops and says what the human can set up. The rule table is owned by the human.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from envrec.mission.models import TargetEntry, Transport
from envrec.skills.data import reference_json, reference_text

_ROUTE_SKILL = {Transport.ssh: "connect-ssh", Transport.docker_exec: "connect-docker-exec", Transport.adb_usb: "connect-adb", Transport.ios_usb: "connect-ios", Transport.serial: "connect-serial", Transport.tcp: "connect-tcp"}
HINTS = {t: reference_text(skill, "setup-hint.txt").strip() for t, skill in _ROUTE_SKILL.items()}  # what the human can set up: written in each route's skill


@dataclass
class PlanResult:
    transport: Transport | None
    decided_by: list[str] = field(default_factory=list)  # the facts and rules that picked it
    considered: list[Transport] = field(default_factory=list)  # preference order, restricted to allowed
    hint: str = ""


def _fires(when: dict, facts: dict[str, Any]) -> bool:
    if "any" in when:
        return any(_fires(w, facts) for w in when["any"])
    value = facts.get(when["fact"])
    if when["fact"] == "os.name":
        value = str(value or "").lower()
    return value in when["in"]


def preference(facts: dict[str, Any]) -> tuple[list[Transport], list[str]]:
    """Rule table (the `connect-route` skill's references/route-table.json): preferred transports in order, plus the facts behind the order."""
    table = reference_json("connect-route", "route-table.json")
    ctx = {"kind": facts.get("host.kind"), "os_name": str(facts.get("os.name") or "").lower()}
    for rule in table["rules"]:
        if _fires(rule["when"], facts):
            return [Transport(t) for t in rule["prefer"]], [w.format(**ctx) for w in rule["why"]]
    d = table["default"]
    return [Transport(t) for t in d["prefer"]], list(d["why"])


def plan_transport(entry: TargetEntry, facts: dict[str, Any], reachable: dict[Transport, bool | None]) -> PlanResult:
    """`reachable` holds results of read-only checks the caller already ran (True / False / None = not checked).
    Only transports in entry.allowed_transports are ever considered."""
    pref, why = preference(facts)
    allowed = list(entry.allowed_transports)
    ordered = [t for t in pref if t in allowed] + [t for t in allowed if t not in pref]
    for t in ordered:
        if reachable.get(t) is True:
            return PlanResult(t, why + [f"{t.value} answered the read-only check"], ordered)
    first = ordered[0]
    return PlanResult(None, why, ordered,
                      hint=f"no allowed transport answered ({', '.join(t.value for t in ordered)}). "
                           f"For {first.value}: {HINTS.get(first, 'nothing to set up')}")


def record_plan(store, target_id: str, plan: PlanResult) -> None:
    """Event: which transport was chosen and the facts that decided it."""
    from envrec.contracts import Actor, Event
    store.append_event(Event(actor=Actor.loop, type="transport_planned", payload={
        "target": target_id, "transport": plan.transport.value if plan.transport else None,
        "decided_by": plan.decided_by, "considered": [t.value for t in plan.considered], "hint": plan.hint}))

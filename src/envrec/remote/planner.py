"""Connection planner: pick the best transport for ONE named target, from that target's allowed list only.

Inputs are the target's own config entry and read-only facts. It never connects, never tries a key, a
port or a transport that the config does not name, and never looks for another way in. If nothing listed
works it stops and says what the human can set up. The rule table is owned by the human.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from envrec.mission.models import TargetEntry, Transport

HINTS = {
    Transport.ssh: "enable sshd on the device (on Android: install Termux and run sshd), add your own public key, "
                   "then re-run",
    Transport.docker_exec: "start the container and make sure the docker CLI can see it, then re-run",
    Transport.adb_usb: "connect the cable, turn on USB debugging in developer options and accept the key prompt on "
                       "the phone, then re-run",
    Transport.serial: "connect the serial adapter, check the port name and permissions, then re-run",
}


@dataclass
class PlanResult:
    transport: Transport | None
    decided_by: list[str] = field(default_factory=list)  # the facts and rules that picked it
    considered: list[Transport] = field(default_factory=list)  # preference order, restricted to allowed
    hint: str = ""


def preference(facts: dict[str, Any]) -> tuple[list[Transport], list[str]]:
    """Rule table: preferred transports in order, plus the facts behind the order."""
    kind, os_name = facts.get("host.kind"), str(facts.get("os.name") or "").lower()
    if kind == "container":
        return [Transport.docker_exec, Transport.ssh], ["host.kind=container"]
    if kind == "phone" or os_name == "android":
        return [Transport.adb_usb, Transport.ssh], [f"host.kind={kind}", f"os.name={os_name}"]
    if kind in ("sbc", "microcontroller"):
        return [Transport.ssh, Transport.serial], [f"host.kind={kind}"]
    if os_name in ("linux", "macos"):
        return [Transport.ssh], [f"os.name={os_name}"]
    return [Transport.ssh, Transport.docker_exec, Transport.adb_usb, Transport.serial], ["no rule matched: allowed order"]


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
                           f"For {first.value}: {HINTS[first]}")


def record_plan(store, target_id: str, plan: PlanResult) -> None:
    """Event: which transport was chosen and the facts that decided it."""
    from envrec.contracts import Actor, Event
    store.append_event(Event(actor=Actor.loop, type="transport_planned", payload={
        "target": target_id, "transport": plan.transport.value if plan.transport else None,
        "decided_by": plan.decided_by, "considered": [t.value for t in plan.considered], "hint": plan.hint}))

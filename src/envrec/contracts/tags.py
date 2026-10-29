"""Who decided: the actor tag every event carries (MODEL, GUARD, SCRIPT, OWNER). docs/HARNESS_SPEC.md defines them.

MODEL  the model chose (an action from the menu, an advance it asked for).
GUARD  code refused, enforced or recorded; it never picked what happens next.
SCRIPT code decided the next action or the outcome (a fallback, a forced advance, a ring move, a repair, a fixed sequence).
OWNER  the human approved or typed an answer.
`Event.tag` is stored; for an event written before the field existed `derive_tag` reproduces it from the actor, type and payload, so old logs still count."""

from __future__ import annotations

from typing import Any

from envrec.contracts.enums import Actor

TAGS = ("MODEL", "GUARD", "SCRIPT", "OWNER")

# `discovery_adapter` is only the RECORD that a skill ran; who chose to run it is told by the event before it: the model's action_chosen, or the loop's first_rung_identify (SCRIPT)
_SCRIPT_TYPES = {"first_rung_identify", "skill_reuse", "skill_reused", "transport_planned", "model_fallback", "model_repaired"}
_MODEL_TYPES = {"model_call", "model_invalid", "research", "skill_proposed", "skill_result", "fact_expanded"}


def derive_tag(actor: Actor | str, type: str, payload: dict[str, Any] | None = None) -> str:
    p = payload or {}
    actor = actor.value if isinstance(actor, Actor) else str(actor)
    if actor == "human":
        return "OWNER"
    if type == "action_chosen":
        return "SCRIPT" if p.get("fallback") else "MODEL"
    if type == "protocol_state" and p.get("by") == "evidence":
        return "GUARD"  # the step's evidence is in (produced by tools the model chose): code only records the move
    if type in ("protocol_state", "ring_extended", "unknown_recorded"):
        if p.get("fallback") or p.get("by") == "code":
            return "SCRIPT"
        return "MODEL" if (p.get("by") == "model" or type != "protocol_state") else "SCRIPT"
    if type in _SCRIPT_TYPES:
        return "SCRIPT"
    if type in _MODEL_TYPES:
        return "MODEL"
    if actor == "worker":
        return "SCRIPT"
    return "GUARD"

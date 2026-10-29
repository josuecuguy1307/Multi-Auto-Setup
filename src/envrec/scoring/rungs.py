"""Highest rung of the unknown ladder each target reached."""

from __future__ import annotations

from envrec.store import Store


def highest_rung(store: Store, target_id: str) -> int:
    """0 = nothing tried. Counts tools run (their rung), hypotheses made, and asking the human (rung 6).
    A fallback record_unknown caused by invalid model replies does not count as reaching rung 6."""
    best = 0
    for e in store.events():
        if e.payload.get("target") != target_id:
            continue
        if isinstance(e.payload.get("rung"), int):  # tool_run, research, probe_proposed
            best = max(best, e.payload["rung"])
        elif e.type == "human_question" or (e.type == "unknown_recorded" and not e.payload.get("fallback")):
            best = max(best, 6)
    for h in store.hypotheses(target_id):
        best = max(best, h.rung)
    return best

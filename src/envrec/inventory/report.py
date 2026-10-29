"""Inventory report: devices seen, what was recognized, what stayed unknown. Writes nothing.

Candidates exist only in memory for the session. Nothing about an unenrolled device is persisted here;
the report prints an anonymized count for them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from envrec.contracts import FactStatus, TargetKind
from envrec.mission.models import TargetConfig
from envrec.store import Store

SELF_KINDS = {TargetKind.host}


@dataclass
class Candidate:
    name: str
    kind: str
    seen_via: str
    host: str | None = None
    facts: dict[str, tuple[Any, str, float]] = field(default_factory=dict)  # key -> (value, status, confidence)
    unknown: list[str] = field(default_factory=list)

    def known_values(self) -> dict[str, Any]:
        return {k: v for k, (v, _s, _c) in self.facts.items()}


def build_candidates(store: Store, config: TargetConfig, *, seen_via: str = "local discovery") -> list[Candidate]:
    """In-memory view of non-host targets the store knows about that are NOT enrolled in targets.yaml."""
    enrolled = {t.id for t in config.targets} | {t.host for t in config.targets}
    out: list[Candidate] = []
    for t in store.list_targets():
        if t.kind in SELF_KINDS or t.handle in enrolled:
            continue
        c = Candidate(name=t.handle, kind=t.kind.value, seen_via=seen_via)
        for f in store.current_facts(t.id):
            if f.key.startswith("ring."):
                continue
            if f.status == FactStatus.unknown:
                c.unknown.append(f.key)
            else:
                conf = max((e.confidence for e in f.evidence), default=0.0)
                c.facts[f.key] = (f.value, f.status.value, conf)
        out.append(c)
    return out


def render_inventory(candidates: list[Candidate], config: TargetConfig) -> str:
    lines = ["# Inventory report", "",
             f"Enrolled devices (in targets.yaml): {len(config.targets)}. "
             f"Seen but not enrolled: {len(candidates)}. Only you can enroll a device.", ""]
    if config.targets:
        lines += ["## Enrolled", ""] + [f"- `{t.id}` at {t.host}, transports {[x.value for x in t.allowed_transports]}, "
                                         f"reachability {t.reachability}" for t in config.targets] + [""]
    lines += ["## Seen, not enrolled", ""]
    if not candidates:
        lines += ["Nothing.", ""]
    for c in candidates:
        lines += [f"### {c.name} ({c.kind})", f"Seen via: {c.seen_via}", "", "Recognized:"]
        lines += [f"- {k} = {v} ({s}, confidence {conf:.2f})" for k, (v, s, conf) in sorted(c.facts.items())] or ["- nothing"]
        lines += ["", "Stayed unknown: " + (", ".join(sorted(c.unknown)) or "nothing"), ""]
    lines += ["This report wrote nothing. To use a device, enroll it (interactive) or add it to targets.yaml by hand.", ""]
    return "\n".join(lines)

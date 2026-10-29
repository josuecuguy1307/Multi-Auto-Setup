"""The view: what the model is told about the store. Facts and questions only; raw output never appears."""

from __future__ import annotations

from typing import Any

from envrec.contracts import Fact, FactStatus, ModelProfile, Target
from envrec.discovery.sanitize import quoted
from envrec.loop.menu import MenuEntry, render_menu, takes_capture
from envrec.loop.prompts import META_ACTIONS
from envrec.store import Store

_WEIGHT = {
    FactStatus.contradicted: 5, FactStatus.verified: 4, FactStatus.tested: 3,
    FactStatus.hypothesis: 2, FactStatus.unknown: 0,
}
_FIRST = ("host.kind", "host.capabilities")  # the facts that decide what comes next
MAX_UNKNOWNS = 4
VALUE_CHARS = 60


def short_value(value: Any, limit: int = VALUE_CHARS) -> str:
    if isinstance(value, bool):
        s = "true" if value else "false"
    elif isinstance(value, list):
        s = ", ".join(map(str, value)) or "(none)"
    elif isinstance(value, dict):
        s = ", ".join(f"{k}:{'no' if v in (None, False) else 'yes'}" for k, v in value.items())
    elif isinstance(value, str):
        return quoted(value, limit)  # a string from the world is shown as quoted data, never as bare text that could read as an instruction
    else:
        s = str(value)
    return s if len(s) <= limit else s[: limit - 3] + "..."


def rank_facts(facts: list[Fact]) -> list[Fact]:
    shown = [f for f in facts if f.status != FactStatus.unknown and not f.key.startswith("ring.")]
    return sorted(shown, key=lambda f: (f.key not in _FIRST, -_WEIGHT[f.status], f.key))


def fact_line(f: Fact) -> str:
    n = len(f.evidence)
    return f"  {f.key} = {short_value(f.value)} ({f.status.value}, {n} source{'s' if n != 1 else ''})"


def build_view(store: Store, target: Target, profile: ModelProfile, menu: list[MenuEntry],
               last_result: str, unknown_reasons: dict[str, str], protocol_text: str = "",
               extra_actions: str = "", mission_text: str = "", skill_text: str = "", candidates: list[Target] | None = None, forward: list[str] | None = None) -> str:
    facts = store.current_facts(target.id)
    ranked = rank_facts(facts)
    budget = profile.view_budget_facts
    lines = ([mission_text] if mission_text else []) + [f"TARGET {target.id} (ring {target.ring}, kind: {target.kind.value}, handle: {target.handle})", "FACTS"]
    lines += [fact_line(f) for f in ranked[:budget]] or ["  (none yet)"]
    if len(ranked) > budget:
        lines.append(f"  (+{len(ranked) - budget} more facts; use expand_fact to see one)")

    unknown = [f for f in facts if f.status == FactStatus.unknown and not f.key.startswith("ring.")]
    if unknown:
        shown = [f"{f.key} ({unknown_reasons.get(f.key, 'tried, could not tell')})" for f in unknown[:MAX_UNKNOWNS]]
        extra = f"; +{len(unknown) - MAX_UNKNOWNS} more" if len(unknown) > MAX_UNKNOWNS else ""
        lines.append("OPEN UNKNOWNS: " + "; ".join(shown) + extra)
    if candidates:  # other targets the model may move to; their handles come from the world, so they are shown quoted
        lines.append("CANDIDATES (choose one with extend_to and its target_id): " + "; ".join(f"{c.id} kind={c.kind.value} handle={quoted(c.handle, 60)}" for c in candidates[:8]))
    caps = store.captures(target.id) if takes_capture(menu) else []
    if caps:  # the ids a tool on the menu can take: name, size and the tool that made it. Never the content.
        lines.append("CAPTURES (stored raw data; a tool takes the id, code reads it): " + "; ".join(f"{c['sha256'][:8]} {c['name']} ({c['size']} bytes, by {c['tool']})" for c in caps[-6:]))
    lines.append(f"LAST RESULT: {last_result}")
    if forward:  # the way forward is always shown, and shown as the exact reply: an empty menu is never a dead end the model has to guess its way out of
        lines.append("WAY FORWARD (this step's evidence is in): " + " or ".join(f'{{"action":"advance","to":"{t}"}}' for t in forward))
    empty = "  (nothing is left to run in this step)" if protocol_text else "  (no tools left)"
    lines += ["MENU", render_menu(menu) if menu else empty, f"OTHER ACTIONS: {META_ACTIONS if not protocol_text else META_ACTIONS.replace(' · done', '')}{extra_actions}",
              "Reply with one Action as JSON."]
    head = [protocol_text] if protocol_text else []
    if skill_text:
        head.append(skill_text)
    return ("\n".join(head) + "\n\n" if head else "") + "\n".join(lines)


def expand_fact_text(fact: Fact, limit: int = 400) -> str:
    """More detail on one fact: full value and evidence summaries. Still no raw output."""
    ev = "; ".join(
        f"{e.provider}/{e.probe} conf {e.confidence:.2f}" + (f" limits: {'; '.join(e.limitations)}" if e.limitations else "")
        for e in fact.evidence
    ) or "no evidence"
    text = f"{fact.key} = {short_value(fact.value, limit)} [{fact.status.value}] evidence: {ev}"
    if fact.alt_values:
        text += f" also claimed: {short_value(fact.alt_values, 120)}"
    return text[:limit * 2]

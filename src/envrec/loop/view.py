"""The view: what the model is told about the store. Facts and questions only; raw output never appears."""

from __future__ import annotations

from typing import Any

from envrec.contracts import CreatedBy, Fact, FactStatus, ModelProfile, Target
from envrec.discovery.sanitize import quoted_counted
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


PINNED_VALUE_CHARS = 2400  # a fact the step's spec always shows is what its choices depend on: it gets room to be read, and its identifiers are whole (they are what the menu refers to)
SCALAR_STR_CHARS = 40
MAX_DEPTH = 4
MAX_ITEMS = 12


def _more(k: int) -> str:
    return f"+{k} more"


def _inline(value: Any, depth: int = 0) -> str:
    """A short, readable rendering of a nested value. Strings are quoted data (from the world, so cleaned and capped); numbers and booleans as they are; a long list or dict shows its first
    items and how many more there are; past MAX_DEPTH only a size."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "none"
    if isinstance(value, str):
        return quoted_counted(value, SCALAR_STR_CHARS)
    if isinstance(value, (list, tuple)):
        if depth >= MAX_DEPTH:
            return f"({len(value)} items)"
        parts = [_inline(x, depth + 1) for x in value[:MAX_ITEMS]] + ([_more(len(value) - MAX_ITEMS)] if len(value) > MAX_ITEMS else [])
        return "[" + ", ".join(parts) + "]"
    if isinstance(value, dict):
        if depth >= MAX_DEPTH:  # past the depth the keys still show (what is there), the values do not (and the count says so)
            keys = list(value)
            return "{" + ", ".join(f"{k}: …" for k in keys[:MAX_ITEMS]) + (f", {_more(len(keys) - MAX_ITEMS)}" if len(keys) > MAX_ITEMS else "") + "}"
        items = list(value.items())
        parts = [f"{k}: {_inline(v, depth + 1)}" for k, v in items[:MAX_ITEMS]] + ([_more(len(items) - MAX_ITEMS)] if len(items) > MAX_ITEMS else [])
        return "{" + ", ".join(parts) + "}"
    return str(value)


def _fit(parts: list[str], limit: int) -> str:
    """The parts joined while they fit; the ones that do not are counted ("+K more"), never dropped silently. The first part is always whole (it already carries its own count if it was cut)."""
    out: list[str] = []
    size = 0
    for i, p in enumerate(parts):
        if out and size + len(p) + 2 > limit - 12:
            return ", ".join(out + [_more(len(parts) - i)])
        out.append(p)
        size += len(p) + 2
    return ", ".join(out)


def short_value(value: Any, limit: int = VALUE_CHARS) -> str:
    if isinstance(value, bool):
        s = "true" if value else "false"
    elif isinstance(value, list) and all(isinstance(x, (int, float, bool, str)) for x in value):
        s = _fit([str(x) for x in value], limit) or "(none)"
    elif isinstance(value, dict):
        s = _fit([f"{k}: {_inline(v, 1)}" for k, v in value.items()], limit)
    elif isinstance(value, list):
        s = _fit([_inline(x, 1) for x in value], limit)
    elif isinstance(value, str):
        return quoted_counted(value, limit)  # a string from the world is shown as quoted data, never as bare text that could read as an instruction; an identifier is whole, any other cut says how much
    else:
        s = str(value)
    return s if len(s) <= limit else f"{s[: limit - 1]}… (+{len(s) - (limit - 1)} chars)"


def rank_facts(facts: list[Fact]) -> list[Fact]:
    shown = [f for f in facts if f.status != FactStatus.unknown and not f.key.startswith("ring.")]
    return sorted(shown, key=lambda f: (f.key not in _FIRST, f.created_by != CreatedBy.human, -_WEIGHT[f.status], f.key))  # what the human stated is never pushed out of the budget by what a tool read


def fact_line(f: Fact, limit: int = VALUE_CHARS) -> str:
    n = len(f.evidence)
    return f"  {f.key} = {short_value(f.value, limit)} ({f.status.value}, {n} source{'s' if n != 1 else ''})"


def way_forward_text(forward: list[str]) -> str:
    return "WAY FORWARD (this step's evidence is in): " + " or ".join(f'{{"action":"advance","to":"{t}"}}' for t in forward)


def build_view(store: Store, target: Target, profile: ModelProfile, menu: list[MenuEntry],
               last_result: str, unknown_reasons: dict[str, str], protocol_text: str = "",
               extra_actions: str = "", mission_text: str = "", skill_text: str = "", candidates: list[Target] | None = None, forward: list[str] | None = None,
               missing_text: str = "", pinned: list[str] | None = None) -> str:
    facts = store.current_facts(target.id)
    ranked = rank_facts(facts)
    budget = profile.view_budget_facts
    keep = set(pinned or [])  # the facts the step's exit is made of (and the ones its spec lists) are always shown, outside the budget: a model cannot judge what it cannot see
    prefixes = tuple(k[:-1] for k in keep if k.endswith("*"))
    is_fixed = lambda f: f.key in keep or (bool(prefixes) and f.key.startswith(prefixes))
    fixed = [f for f in ranked if is_fixed(f)]
    rest = [f for f in ranked if not is_fixed(f)]
    lines = ([mission_text] if mission_text else []) + [f"TARGET {target.id} (ring {target.ring}, kind: {target.kind.value}, handle: {target.handle})", "FACTS"]
    lines += [fact_line(f, PINNED_VALUE_CHARS) for f in fixed] + [fact_line(f) for f in rest[:budget]] or ["  (none yet)"]
    if len(rest) > budget:
        lines.append(f"  (+{len(rest) - budget} more facts; use expand_fact to see one)")

    unknown = [f for f in facts if f.status == FactStatus.unknown and not f.key.startswith("ring.")]
    if unknown:
        shown = [f"{f.key} ({unknown_reasons.get(f.key, 'tried, could not tell')})" for f in unknown[:MAX_UNKNOWNS]]
        extra = f"; +{len(unknown) - MAX_UNKNOWNS} more" if len(unknown) > MAX_UNKNOWNS else ""
        lines.append("OPEN UNKNOWNS: " + "; ".join(shown) + extra)
    if candidates:  # other targets the model may move to; their handles come from the world, so they are shown quoted
        lines.append("CANDIDATES (choose one with extend_to and its target_id): " + "; ".join(f"{c.id} kind={c.kind.value} handle={quoted_counted(c.handle, 60)}" for c in candidates[:8]))
    caps = store.captures(target.id) if takes_capture(menu) else []
    if caps:  # the ids a tool on the menu can take: name, size and the tool that made it. Never the content.
        lines.append("CAPTURES (stored raw data; a tool takes the id, code reads it): " + "; ".join(f"{c['sha256'][:8]} {c['name']} ({c['size']} bytes, by {c['tool']})" for c in caps[-6:]))
    lines.append(f"LAST RESULT: {last_result}")
    if missing_text:  # what the exit still lacks and which tool on this target makes it: named, never run for the model
        lines.append("STILL MISSING: " + missing_text)
    empty = "  (nothing is left to run in this step)" if protocol_text else "  (no tools left)"
    if forward:  # the way forward is always shown, and shown as the exact reply, above the menu, where it always was: an empty menu is never a dead end to guess out of
        lines.append(way_forward_text(forward))
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

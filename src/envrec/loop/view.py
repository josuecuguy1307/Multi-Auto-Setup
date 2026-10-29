"""The view: what the model is told about the store. Facts and questions only; raw output never appears."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from envrec.contracts import Fact, FactStatus, ModelProfile, Target
from envrec.loop.menu import MenuEntry, render_menu
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
    else:
        s = str(value)
    return s if len(s) <= limit else s[: limit - 3] + "..."


def rank_facts(facts: list[Fact]) -> list[Fact]:
    shown = [f for f in facts if f.status != FactStatus.unknown and not f.key.startswith("ring.")]
    return sorted(shown, key=lambda f: (f.key not in _FIRST, -_WEIGHT[f.status], f.key))


def fact_line(f: Fact) -> str:
    n = len(f.evidence)
    return f"  {f.key} = {short_value(f.value)} ({f.status.value}, {n} source{'s' if n != 1 else ''})"


def playbook_text(profile: ModelProfile, target: Target, playbook_dir: Path) -> str:
    """One-page card per target kind, shown only to profiles that list it."""
    if target.kind.value not in profile.playbooks:
        return ""
    path = playbook_dir / f"{target.kind.value}.md"
    return path.read_text(encoding="utf-8").strip() + "\n\n" if path.is_file() else ""


def build_view(store: Store, target: Target, profile: ModelProfile, menu: list[MenuEntry],
               last_result: str, unknown_reasons: dict[str, str], playbook_dir: Path | None = None,
               extra_actions: str = "", mission_text: str = "") -> str:
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
    lines.append(f"LAST RESULT: {last_result}")
    lines += ["MENU", render_menu(menu) if menu else "  (no tools left)", f"OTHER ACTIONS: {META_ACTIONS}{extra_actions}",
              "Reply with one Action as JSON."]
    card = playbook_text(profile, target, playbook_dir) if playbook_dir else ""
    return card + "\n".join(lines)


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

"""The menu: a closed, short list of tools that fit the current ring, target kind and state."""

from __future__ import annotations

from dataclasses import dataclass

from envrec.adapters import Adapter
from envrec.contracts import Decision, Target, ToolSpec
from envrec.policy import PolicyState, decide
from envrec.store import Store


@dataclass(frozen=True)
class MenuEntry:
    name: str
    one_line: str
    spec: ToolSpec
    adapter: Adapter


def eligible_tools(adapters: list[Adapter], target: Target, store: Store,
                   state: PolicyState | None = None) -> list[MenuEntry]:
    """Every tool that fits this target right now, in adapter order. Not yet capped."""
    have = {f.key for f in store.current_facts(target.id)}  # known OR tried-and-unknown
    ran = store.tools_run(target.id)
    out: list[MenuEntry] = []
    for adapter in adapters:
        for spec in adapter.tool_specs():
            if target.kind not in spec.target_kinds or spec.ring > target.ring:
                continue
            if not spec.repeatable and (spec.produces or spec.source == "skills" or not spec.input_model.get("required")) and spec.name in ran:
                continue  # already ran here; asking again would not change the answer (a tool that takes no required argument cannot be asked a different question)
            if any(k not in have for k in spec.requires):
                continue  # prerequisites not in yet
            if not adapter.available(spec.name, target, store):
                continue  # the adapter says not now
            if decide(spec, target, state).decision == Decision.deny:
                continue  # never offer what policy would refuse
            out.append(MenuEntry(spec.name, spec.one_line, spec, adapter))
    return out


def build_menu(adapters: list[Adapter], target: Target, store: Store, menu_max: int,
               state: PolicyState | None = None, allow=None, missing: set[str] | None = None) -> list[MenuEntry]:
    """The closed list shown to the model: at most `menu_max` tools. `allow` is the protocol's filter for the current step, applied before the cap. `missing` is the fact keys the step's
    exit still lacks: the tools that produce one are kept ahead of the cap (a stable reorder; nothing is added, removed or chosen)."""
    entries = eligible_tools(adapters, target, store, state)
    if allow is not None:
        entries = [e for e in entries if allow(e)]
    if missing:
        entries = sorted(entries, key=lambda e: not (set(e.spec.produces) & missing))
    return entries[:menu_max]


def _arg_lines(spec: ToolSpec) -> list[str]:
    """One line per argument the tool takes: what to put there and where the value comes from. The model is told its arguments; it cannot fill what it cannot see."""
    props = spec.input_model.get("properties") or {}
    required = set(spec.input_model.get("required") or [])
    lines = []
    for name, p in props.items():
        where = {"capture": "a capture id from CAPTURES", "fact": "a fact key from FACTS"}.get(p.get("x-ref", ""), p.get("type", "value"))
        lines.append(f"      {name} ({'required' if name in required else 'optional'}, {where}): {str(p.get('description', ''))[:240]}")
    return lines


def takes_capture(entries: list[MenuEntry]) -> bool:
    """Does any tool on the menu take a capture id? Then the view lists the captures that exist."""
    return any(p.get("x-ref") == "capture" for e in entries for p in (e.spec.input_model.get("properties") or {}).values())


def render_menu(entries: list[MenuEntry]) -> str:
    out = []
    for i, e in enumerate(entries, 1):
        props = e.spec.input_model.get("properties") or {}
        sig = f"({', '.join(props)})" if props else ""  # names exactly as the model must write them in "args": no decoration it could copy
        out.append(f"  {i} {e.name}{sig}  {(e.spec.description or e.one_line)[:200]}")
        out += _arg_lines(e.spec)
    return "\n".join(out)

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
            if spec.produces and spec.name in ran:
                continue  # already ran here; asking again would not change the answer
            if any(k not in have for k in spec.requires):
                continue  # prerequisites not in yet
            if not adapter.available(spec.name, target, store):
                continue  # the adapter says not now
            if decide(spec, target, state).decision == Decision.deny:
                continue  # never offer what policy would refuse
            out.append(MenuEntry(spec.name, spec.one_line, spec, adapter))
    return out


def build_menu(adapters: list[Adapter], target: Target, store: Store, menu_max: int,
               state: PolicyState | None = None) -> list[MenuEntry]:
    """The closed list shown to the model: at most `menu_max` tools."""
    return eligible_tools(adapters, target, store, state)[:menu_max]


def render_menu(entries: list[MenuEntry]) -> str:
    return "\n".join(f"  {i} {e.name}  {e.one_line}" for i, e in enumerate(entries, 1))

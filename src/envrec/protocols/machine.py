"""Protocols as an enforced state machine.

A protocol is a skill (a `SKILL.md` folder whose sidecar carries `states`). The body is the guidance the model reads for a step; the states, the tool tags allowed in each
one and the evidence required to move on are enforced HERE, in code, by the loop: the menu is cut to the current state, and a request to advance without the evidence is
refused with a typed message and counted. Judgment stays in markdown; constraints do not.

Evidence a transition can require (each entry of a `needs` list; all must hold):
  {fact: KEY, at_least: hypothesis|tested|verified}   a fact with that status or better (default tested)
  {tried: KEY}                                         a fact exists in any status, an unknown the probe could not fill included: the attempt was made
  {tool: NAME}                                         that tool or skill has run on this target
  {tool_tag: TAG}                                      some tool carrying that tag has run on this target
  {action: NAME}                                       the model took that action in this protocol run (research, propose_skill, ask_human)
  {facts_prefix: "usb.", at_least_n: N, status_min: tested}   at least N facts under a prefix
  {any: [need, ...]}                                   at least one of the listed needs holds
"""

from __future__ import annotations

from dataclasses import dataclass, field

from envrec.contracts import FactStatus
from envrec.contracts.enums import STATUS_RANK
from envrec.skills.meta import ProtocolDef, State
from envrec.skills.skillmd import Skill

GENERIC = "generic"
META_ACTIONS = {"expand_fact", "record_unknown", "ask_human"}  # allowed in every non-terminal state; the others a state lists explicitly


@dataclass
class EvidenceView:
    """What the machine may look at: the target's current facts, the tools that ran on it, the tags of every tool, the actions taken."""

    facts: dict[str, object] = field(default_factory=dict)  # key -> Fact
    tools_run: set[str] = field(default_factory=set)
    tool_tags: dict[str, set[str]] = field(default_factory=dict)
    actions: set[str] = field(default_factory=set)


@dataclass
class Check:
    ok: bool
    missing: list[str] = field(default_factory=list)


def _rank(status: FactStatus) -> int:
    return 4 if status == FactStatus.contradicted else STATUS_RANK[status]  # a measured disagreement is a result: it counts as at least tested


def _met(need: dict, ev: EvidenceView) -> tuple[bool, str]:
    if "fact" in need:
        key, floor = need["fact"], need.get("at_least", "tested")
        f = ev.facts.get(key)
        ok = f is not None and f.status != FactStatus.unknown and _rank(f.status) >= STATUS_RANK[FactStatus(floor)]
        return ok, f"{key} ({floor} or better)"
    if "tried" in need:
        return need["tried"] in ev.facts, f"{need['tried']} (known, or recorded as unknown after a try)"
    if "tool" in need:
        return need["tool"] in ev.tools_run, f"the tool {need['tool']} to have run"
    if "tool_tag" in need:
        tag = need["tool_tag"]
        return any(tag in ev.tool_tags.get(t, set()) for t in ev.tools_run), f"a tool tagged {tag} to have run"
    if "action" in need:
        return need["action"] in ev.actions, f"the action {need['action']} to have been taken"
    if "facts_prefix" in need:
        pre, n, floor = need["facts_prefix"], int(need.get("at_least_n", 1)), need.get("status_min", "tested")
        have = [k for k, f in ev.facts.items() if k.startswith(pre) and f.status != FactStatus.unknown and _rank(f.status) >= STATUS_RANK[FactStatus(floor)]]
        return len(have) >= n, f"{n} fact(s) under {pre}* ({floor} or better)"
    if "any" in need:
        results = [_met(n, ev) for n in need["any"]]
        return any(ok for ok, _ in results), "one of: " + " | ".join(m for _, m in results)
    return False, f"an unreadable requirement {need!r}"


class ProtocolRun:
    """One target's walk through one protocol."""

    def __init__(self, skill: Skill) -> None:
        d = skill.meta.states
        assert d is not None, f"{skill.name} is not a protocol"
        self.skill, self.defn, self.id = skill, d, skill.meta.protocol
        self.state = d.start
        self.refusals = 0
        self.taken: set[str] = set()  # model actions taken in this run (research, propose_skill, ask_human)
        self.history: list[dict] = [{"state": self.state, "by": "start"}]

    @property
    def current(self) -> State:
        return self.defn.states[self.state]

    @property
    def terminal(self) -> bool:
        return self.current.terminal

    def allows_action(self, action: str) -> bool:
        if self.terminal:
            return False
        return action in META_ACTIONS or action in ("advance", "call_tool", "done", "extend_to") or action in self.current.actions

    def allows_entry(self, name: str, tags: list[str], protocol_of_skill: str | None = None) -> bool:
        """A menu entry is offered when the state lists its name, or shares one of the state's tags. A skill is also offered in the state that lists the tag `skill`
        when it belongs to this protocol."""
        if self.terminal:
            return False
        st = self.current
        if name in st.tools:
            return True
        if name.startswith("skill."):
            return "skill" in st.tags and (protocol_of_skill in (None, self.id))
        return bool(set(tags) & set(st.tags))

    def check(self, to: str, ev: EvidenceView) -> Check:
        rules = [a for a in self.current.advance if a.to == to]
        if not rules:
            ok_to = sorted({a.to for a in self.current.advance})
            return Check(False, [f"no transition from {self.state} to {to}; from here you can go to {ok_to or 'nowhere'}"])
        best: list[str] | None = None
        for r in rules:
            miss = [m for ok, m in (_met(n, ev) for n in r.needs) if not ok]
            if not miss:
                return Check(True)
            best = miss if best is None or len(miss) < len(best) else best
        return Check(False, best or [])

    def ready(self, ev: EvidenceView) -> str | None:
        """The first state whose evidence is already in (what code may advance to by itself when the menu has nothing left)."""
        for a in self.current.advance:
            if all(ok for ok, _ in (_met(n, ev) for n in a.needs)):
                return a.to
        return None

    def missing_for_exit(self, ev: EvidenceView) -> list[str]:
        """Fact keys the first exit still needs and that a try could settle (what code records as unknown when no tool is left in this step)."""
        out: list[str] = []
        for a in self.current.advance[:1]:
            for n in a.needs:
                if not _met(n, ev)[0]:
                    key = n.get("fact") or n.get("tried")
                    if key:
                        out.append(key)
        return out

    def move(self, to: str, by: str) -> None:
        self.state = to
        self.history.append({"state": to, "by": by})

    def refuse(self, to: str, miss: list[str]) -> str:
        self.refusals += 1
        return f"refused: cannot advance from {self.state} to {to}; it needs " + "; ".join(miss) + f" (refusal {self.refusals})"

    def card(self) -> str:
        st = self.current
        names = list(self.defn.states)
        lines = [f"PROTOCOL {self.id}, STEP {self.state} ({names.index(self.state) + 1} of {len(names)}): {st.goal}".rstrip(": ")]
        if st.terminal:
            lines.append(f"This protocol has ended: {st.reason or 'nothing is left to do here'}. Reply done.")
        else:
            for a in st.advance:
                needs = []
                for n in a.needs:
                    key = n.get("fact") or n.get("tried") or n.get("tool") or n.get("tool_tag") or n.get("action") or n.get("facts_prefix") or "one of several"
                    needs.append(str(key))
                lines.append(f"TO MOVE ON: reply advance to {a.to}" + (f" once you have {', '.join(needs)}." if needs else "."))
        return "\n".join(lines)


class ProtocolSet:
    """The protocols of the library, resolved by target kind."""

    def __init__(self, skills: list[Skill]) -> None:
        self.by_id = {s.meta.protocol: s for s in skills if s.meta.states is not None}
        self.by_kind: dict[str, Skill] = {}
        for s in self.by_id.values():
            for k in s.meta.states.kinds:  # type: ignore[union-attr]
                self.by_kind[k] = s

    @classmethod
    def from_library(cls, lib=None) -> ProtocolSet:
        from envrec.skills.lib import Library
        return cls((lib or Library().load()).protocols())

    def for_kind(self, kind: str) -> Skill | None:
        return self.by_kind.get(kind) or self.by_id.get(GENERIC)

    def start(self, kind: str) -> ProtocolRun | None:
        s = self.for_kind(kind)
        return ProtocolRun(s) if s else None


__all__ = ["Check", "EvidenceView", "ProtocolDef", "ProtocolRun", "ProtocolSet", "State"]

"""The privacy gate: the ONE place where anything a tool or a skill produced becomes a stored capture, fact or entity.

Reducers run here, automatically: captures go through their named reducer before the raw store, every string is cleaned (control characters, secrets, masked
identifiers, capped), dict keys named like arguments, environment or credentials are dropped, identifiers are salted-hashed (stable salt for things the human
owns, a per-run salt for ambient ones) and never stored. Skills and tools return typed outputs; only this module turns them into facts.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from envrec.contracts import CreatedBy, Evidence, Fact, FactStatus, Target
from envrec.discovery.sanitize import Salts, clean_attrs, clean_text
from envrec.discovery.types import KIND_PREFIX, Capture, Entity, FactOut, UnknownOut, entity_key, short_hash
from envrec.discovery.volatile import is_volatile
from envrec.privacy.reducers import clean_value, reduce_capture_bytes
from envrec.store import Store


@dataclass
class CommitSummary:
    facts: int = 0
    entities: int = 0
    resolved: int = 0
    unknowns: int = 0
    unknown_reasons: dict[str, str] = field(default_factory=dict)
    dropped: list[str] = field(default_factory=list)


class PrivacyGate:
    def __init__(self, store: Store, target: Target | None, salts: Salts, provider: str, *, event_id: str | None = None,
                 reducers: dict[str, str | None] | None = None, origin: str = "") -> None:
        self.store, self.target, self.salts, self.provider = store, target, salts, provider[:64]
        self.event_id, self.reducers, self.origin = event_id, reducers or {}, origin
        self.refs: dict[str, object] = {}
        self.by_name: dict[str, Capture] = {}
        self.first_ok: str | None = None

    # ---- captures ------------------------------------------------------------------------------------------------------------

    def store_captures(self, captures: list[Capture]) -> None:
        for c in captures:
            data = reduce_capture_bytes(c.data, self.reducers.get(c.name))
            self.refs[c.name] = self.store.put_raw(data)
            self.by_name[c.name] = c
            if self.first_ok is None and c.error is None:
                self.first_ok = c.name

    def _evidence(self, src: str | None, conf: float, lim: list[str]) -> Evidence:
        name = src if src in self.by_name else self.first_ok
        cap = self.by_name[name]
        return Evidence(provider=self.provider, probe=f"{self.provider}:{cap.name}"[:96], event_id=self.event_id, raw_ref=self.refs[name], confidence=conf,
                        limitations=[clean_text(x, 120) for x in lim][:6], note=f"{cap.origin}: {cap.source}"[:240])

    # ---- the fact boundary ---------------------------------------------------------------------------------------------------

    def commit(self, outs: list[FactOut | Entity | UnknownOut]) -> CommitSummary:
        """Build the cleaned facts and write them."""
        facts, s = self.build(outs)
        for f in facts:
            self.store.write_fact(f)
        return s

    def write_facts(self, facts: list[Fact]) -> list[Fact]:
        """Facts built elsewhere (a legacy adapter, the loop itself) cross the same boundary: values cleaned, then written."""
        out = []
        for f in facts:
            if f.value is not None:
                f = f.model_copy(update={"value": clean_value(f.value, cap=200)})
            out.append(self.store.write_fact(f))
        return out

    def build(self, outs: list[FactOut | Entity | UnknownOut]) -> tuple[list[Fact], CommitSummary]:
        s = CommitSummary()
        built: list[Fact] = []
        for out in outs:
            try:
                if isinstance(out, FactOut):
                    lim = list(out.limitations) + (["volatile: changes on its own"] if is_volatile(out.key) else [])
                    st = FactStatus.hypothesis if out.hypothesis else FactStatus.tested
                    built.append(Fact(target_id=self.target.id, key=out.key, value=clean_value(out.value, cap=200), status=st,
                                      evidence=[self._evidence(out.src, out.confidence, lim)], created_by=CreatedBy.adapter))
                    s.facts += 1
                elif isinstance(out, Entity):
                    salt = self.salts.stable if out.owned == "owned" else self.salts.run
                    eid = f"{out.kind}-{short_hash(out.ident, salt=salt)}".replace("_", "-")
                    prefix = out.prefix or KIND_PREFIX.get(out.kind, "app")
                    attrs = clean_value(clean_attrs(out.attrs), cap=80)
                    if out.links:
                        attrs["links"] = [f"{k}-{short_hash(i, salt=salt)}".replace("_", "-") for k, i in out.links]
                    value = {"kind": out.kind, "ring": out.ring, "owned": out.owned, "resolved": out.resolved, "attrs": attrs}
                    lim = [] if out.resolved else ["not resolved by any reference table: also an Unknown"]
                    ev = self._evidence(out.src, out.confidence if out.resolved else min(out.confidence, 0.5), lim)
                    st = FactStatus.hypothesis if (out.hypothesis or not out.resolved) else FactStatus.tested
                    built.append(Fact(target_id=self.target.id, key=entity_key(prefix, eid), value=value, status=st, evidence=[ev], created_by=CreatedBy.adapter))
                    s.entities += 1
                    s.resolved += int(out.resolved)
                    if not out.resolved:
                        s.unknowns += 1
                elif isinstance(out, UnknownOut):
                    built.append(Fact(target_id=self.target.id, key=out.key, created_by=CreatedBy.adapter))
                    s.unknowns += 1
                    s.unknown_reasons[out.key] = f"{out.reason.value}: {clean_text(out.detail, 100)}"
            except Exception as exc:  # one bad output (an invalid key, say) must not drop the others
                s.dropped.append(f"{type(exc).__name__}: {str(exc)[:80]}")
        return built, s

"""The status record every discovery run leaves behind (`ring.adapter.<id>`), plus the AdapterRun summary the CLI prints. Collection and extraction live in skills."""

from __future__ import annotations

from dataclasses import dataclass, field

from envrec.contracts import Actor, CreatedBy, Event, Evidence, Fact, FactStatus, Target
from envrec.store import Store


@dataclass
class AdapterRun:
    adapter_id: str
    ring: int
    status: str  # ran | unavailable | denied | not_applicable | timeout | permission | error
    reason: str = ""
    facts: int = 0
    entities: int = 0
    resolved: int = 0
    unknowns: int = 0
    captures: int = 0
    seconds: float = 0.0
    unknown_reasons: dict[str, str] = field(default_factory=dict)


def _finish(adapter, store: Store, target: Target, run: AdapterRun, t0: float) -> AdapterRun:
    import time
    run.seconds = round(time.monotonic() - t0, 2)
    value = {"status": run.status, "reason": run.reason[:200], "discovered": run.entities, "resolved": run.resolved,
             "unknown": run.unknowns, "facts": run.facts, "captures": run.captures}
    ev = Evidence(provider="discovery", probe=f"adapter_status:{adapter.id}"[:96], confidence=1.0)
    store.write_fact(Fact(target_id=target.id, key=f"ring.adapter.{adapter.id}", value=value, status=FactStatus.tested, evidence=[ev], created_by=CreatedBy.loop))
    store.append_event(Event(actor=Actor.loop, type="discovery_adapter", payload={"target": target.id, "adapter": adapter.id, "ring": adapter.ring, **value}))
    return run

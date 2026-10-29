"""Closing a ring: write `ring.<n>.coverage` and one decision (deepen, extend, stop)."""

from __future__ import annotations

from envrec.contracts import Evidence, FactStatus, RingCoverage, RingDecision, Target
from envrec.store import Store


def close_ring(
    store: Store,
    target: Target,
    ring: int,
    expected_keys: list[str],
    *,
    further_rings: bool,
    event_id: str | None = None,
) -> RingCoverage:
    """What did this ring check, what stays unknown, and what next.

    - deepen: some expected key was never even tried. Stay in this ring.
    - extend: everything was tried and a further ring exists. Move outward.
    - stop: everything was tried and there is nowhere further to go.
    A key that was tried and came back unknown is reported as unknown but does not keep the
    ring open: asking the same probe again would not change the answer.
    """
    checked: list[str] = []
    unknown: list[str] = []
    untried = False
    for key in expected_keys:
        fact = store.current_fact(target.id, key)
        if fact is None:
            untried = True
            unknown.append(key)
        elif fact.status == FactStatus.unknown:
            unknown.append(key)
        else:
            checked.append(key)
    if untried:
        decision = RingDecision.deepen
    else:
        decision = RingDecision.extend if further_rings else RingDecision.stop
    coverage = RingCoverage(ring=ring, checked=checked, unknown=unknown, decision=decision)
    evidence = Evidence(provider="loop", probe="close_ring", event_id=event_id, confidence=1.0)
    store.write_fact(coverage.to_fact(target.id, evidence))
    return coverage

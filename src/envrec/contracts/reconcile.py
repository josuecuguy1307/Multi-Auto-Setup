"""Supersede and contradiction rules, as pure functions over Facts."""

from __future__ import annotations

from dataclasses import dataclass

from envrec.contracts.enums import STATUS_RANK, FactStatus
from envrec.contracts.models import Evidence, Fact, independent_observations


@dataclass(frozen=True)
class Reconciled:
    fact: Fact  # the fact that is current after this step
    superseded: Fact | None  # the older fact it replaces, if any
    changed: bool  # False when the incoming fact added nothing


def _evidence_key(e: Evidence) -> tuple:
    return (e.provider, e.probe, e.raw_ref.sha256 if e.raw_ref else None, e.note)


def _merge_evidence(a: list[Evidence], b: list[Evidence]) -> list[Evidence]:
    seen: set[tuple] = set()
    merged: list[Evidence] = []
    for e in [*a, *b]:
        k = _evidence_key(e)
        if k not in seen:
            seen.add(k)
            merged.append(e)
    return merged


def reconcile(current: Fact | None, incoming: Fact) -> Reconciled:
    """Decide what the current fact for (target, key) is after `incoming` arrives.

    - nothing before, or an unknown before: the incoming fact is current.
    - incoming is unknown: nothing changes (an unknown never overwrites knowledge).
    - same value: evidence is merged. Two independent providers that both reached
      `tested` make it `verified`. A hypothesis plus a confirming test stays `tested`.
    - different value from the same provider(s): a re-reading, the newer fact supersedes.
    - different value from different providers: status becomes `contradicted`, both
      evidence entries are kept and the competing values are listed in `alt_values`.
    """
    if current is None or current.status == FactStatus.unknown:
        return Reconciled(
            incoming.model_copy(update={"supersedes": current.id if current else incoming.supersedes}),
            current,
            True,
        )
    if incoming.status == FactStatus.unknown:
        return Reconciled(current, None, False)

    evidence = _merge_evidence(current.evidence, incoming.evidence)

    if current.status == FactStatus.contradicted:
        alts = list(current.alt_values)
        if incoming.value != current.value and incoming.value not in alts:
            alts.append(incoming.value)
        if len(evidence) == len(current.evidence) and alts == list(current.alt_values):
            return Reconciled(current, None, False)
        return Reconciled(_rebuild(current, evidence=evidence, alt_values=alts), current, True)

    if incoming.value == current.value:
        status = max(current.status, incoming.status, key=lambda s: STATUS_RANK[s])
        both_measured = (
            STATUS_RANK[current.status] >= STATUS_RANK[FactStatus.tested]
            and STATUS_RANK[incoming.status] >= STATUS_RANK[FactStatus.tested]
        )
        captures, channels = independent_observations(evidence)  # distinct raw captures AND distinct channels; provider names alone prove nothing
        if both_measured and captures >= 2 and channels >= 2:
            status = FactStatus.verified
        if status == current.status and len(evidence) == len(current.evidence):
            return Reconciled(current, None, False)
        return Reconciled(
            _rebuild(current, evidence=evidence, status=status, created_by=incoming.created_by),
            current,
            True,
        )

    # A different value from the very same source(s) is that source re-reading a changing
    # value (free disk, ring coverage), not two sources disagreeing: the newer reading replaces.
    if {e.provider for e in incoming.evidence} == {e.provider for e in current.evidence}:
        return Reconciled(incoming.model_copy(update={"supersedes": current.id}), current, True)

    return Reconciled(
        _rebuild(
            current,
            evidence=evidence,
            status=FactStatus.contradicted,
            alt_values=[*current.alt_values, incoming.value],
        ),
        current,
        True,
    )


def _rebuild(base: Fact, **updates) -> Fact:
    """New Fact (new id) that supersedes `base`, validated from scratch."""
    data = base.model_dump()
    data.update(updates)
    data.pop("id")
    data.pop("ts")
    data["supersedes"] = base.id
    return Fact.model_validate(data)

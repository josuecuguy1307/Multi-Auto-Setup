"""Per source: how many identifiers matched, how many were confirmed, how many were wrong."""

from __future__ import annotations

from dataclasses import dataclass

from envrec.contracts import HypothesisOutcome
from envrec.store import Store


@dataclass
class SourceStats:
    source: str
    matched: int = 0  # hypotheses this source produced
    confirmed: int = 0  # a confirming read agreed
    wrong: int = 0  # a confirming read contradicted it
    pending: int = 0  # not yet tested


def fingerprint_stats(store: Store) -> dict[str, SourceStats]:
    stats: dict[str, SourceStats] = {}
    for hyp in store.hypotheses():
        if not hyp.source:
            continue
        s = stats.setdefault(hyp.source, SourceStats(hyp.source))
        s.matched += 1
        if hyp.outcome == HypothesisOutcome.confirmed:
            s.confirmed += 1
        elif hyp.outcome == HypothesisOutcome.contradicted:
            s.wrong += 1
        else:
            s.pending += 1
    return stats


def render_stats(stats: dict[str, SourceStats]) -> str:
    if not stats:
        return "no list-based hypotheses were made"
    rows = [f"{s.source:28} matched {s.matched}  confirmed {s.confirmed}  wrong {s.wrong}  pending {s.pending}"
            for s in sorted(stats.values(), key=lambda x: x.source)]
    return "\n".join(rows)

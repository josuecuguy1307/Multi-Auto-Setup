"""Score a store's facts against a ground truth.

Per key: correct, wrong, missing (never tried), unknown (tried, undeterminable) or
contradicted. The false-verified rate is wrong values among facts claimed `verified`.
Every report is labelled PROVISIONAL until the ground truth is fully confirmed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from envrec.contracts import Fact, FactStatus
from envrec.scoring.groundtruth import GroundTruth
from envrec.store import Store

Outcome = Literal["correct", "wrong", "missing", "unknown", "contradicted"]
DEFAULT_REL_TOLERANCE = 0.05
OUTCOMES: tuple[Outcome, ...] = ("correct", "wrong", "missing", "unknown", "contradicted")


@dataclass(frozen=True)
class RowScore:
    key: str
    expected: Any
    actual: Any
    status: str | None
    outcome: Outcome
    confirmed: bool
    note: str = ""


@dataclass
class ScoreReport:
    rows: list[RowScore] = field(default_factory=list)
    unscored: list[str] = field(default_factory=list)
    machine: str = ""
    provisional: bool = True

    def count(self, outcome: Outcome) -> int:
        return sum(1 for r in self.rows if r.outcome == outcome)

    @property
    def verified_total(self) -> int:
        return sum(1 for r in self.rows if r.status == "verified" and r.outcome in ("correct", "wrong"))

    @property
    def verified_wrong(self) -> int:
        return sum(1 for r in self.rows if r.status == "verified" and r.outcome == "wrong")

    @property
    def false_verified_rate(self) -> float | None:
        return None if self.verified_total == 0 else self.verified_wrong / self.verified_total

    def to_dict(self) -> dict[str, Any]:
        return {
            "machine": self.machine,
            "provisional": self.provisional,
            "counts": {o: self.count(o) for o in OUTCOMES},
            "false_verified_rate": self.false_verified_rate,
            "verified_total": self.verified_total,
            "unscored": self.unscored,
            "rows": [r.__dict__ for r in self.rows],
        }

    def render(self) -> str:
        head = ["KEY", "EXPECTED", "ACTUAL", "STATUS", "RESULT", "NOTE"]
        body = [[r.key, _fmt(r.expected), _fmt(r.actual), r.status or "-", r.outcome.upper(), r.note]
                for r in self.rows]
        widths = [max(len(row[i]) for row in [head, *body]) for i in range(len(head))]
        line = lambda cells: "  ".join(c.ljust(w) for c, w in zip(cells, widths)).rstrip()  # noqa: E731
        fv = "n/a (nothing verified)" if self.false_verified_rate is None else (
            f"{self.false_verified_rate:.0%} ({self.verified_wrong} of {self.verified_total} verified are wrong)")
        out = [line(head), line(["-" * w for w in widths]), *[line(r) for r in body], ""]
        out.append(" · ".join(f"{o} {self.count(o)}" for o in OUTCOMES) + f"   (scored {len(self.rows)})")
        out.append(f"false-verified rate: {fv}")
        out.append(f"unscored facts (in store, not in ground truth): {len(self.unscored)}")
        if self.provisional:
            out.append("PROVISIONAL: the ground truth is not fully confirmed by a human yet.")
        return "\n".join(out)


def score_target(store: Store, target_id: str, truth: GroundTruth,
                 *, only_keys: set[str] | None = None) -> ScoreReport:
    facts = {f.key: f for f in store.current_facts(target_id)}
    keys = [k for k in truth.facts if only_keys is None or k in only_keys]
    rows = [_score_key(k, truth.facts[k].value, facts.get(k), truth.facts[k].confirmed,
                       truth.facts[k].rel_tolerance, truth.facts[k].unknown) for k in keys]
    return ScoreReport(
        rows=rows,
        unscored=sorted(k for k in facts if k not in truth.facts and facts[k].status != FactStatus.unknown),
        machine=truth.machine,
        provisional=not truth.fully_confirmed,
    )


def _score_key(key: str, expected: Any, fact: Fact | None, confirmed: bool, tol: float | None,
               expect_unknown: bool = False) -> RowScore:
    if expect_unknown:
        expected = "(unknown)"
        if fact is None:
            return RowScore(key, expected, None, None, "missing", confirmed, "never tried")
        if fact.status == FactStatus.unknown:
            return RowScore(key, expected, None, fact.status.value, "correct", confirmed, "honestly unknown")
        if fact.status == FactStatus.contradicted:
            return RowScore(key, expected, fact.value, fact.status.value, "contradicted", confirmed)
        return RowScore(key, expected, fact.value, fact.status.value, "wrong", confirmed,
                        "claimed a value for something that cannot be known")
    if fact is None:
        return RowScore(key, expected, None, None, "missing", confirmed, "never tried")
    status = fact.status.value
    if fact.status == FactStatus.unknown:
        return RowScore(key, expected, None, status, "unknown", confirmed)
    if fact.status == FactStatus.contradicted:
        return RowScore(key, expected, fact.value, status, "contradicted", confirmed,
                        f"also claimed: {fact.alt_values}")
    ok = values_match(expected, fact.value, DEFAULT_REL_TOLERANCE if tol is None else tol)
    return RowScore(key, expected, fact.value, status, "correct" if ok else "wrong", confirmed)


def values_match(expected: Any, actual: Any, rel_tolerance: float = DEFAULT_REL_TOLERANCE) -> bool:
    """bool exact; ints exact; floats within a relative tolerance; strings case-insensitive;
    lists as sets; dicts entry by entry (an expected bool means 'present' for a non-null actual)."""
    if actual is None or expected is None:
        return False
    if isinstance(expected, bool) or isinstance(actual, bool):
        return isinstance(expected, bool) and isinstance(actual, bool) and expected == actual
    if _num(expected) and _num(actual):
        if isinstance(expected, int) and isinstance(actual, int):
            return expected == actual
        return abs(actual - expected) <= rel_tolerance * max(abs(expected), 1e-9)
    if isinstance(expected, list) and isinstance(actual, list):
        return sorted(_norm(x) for x in expected) == sorted(_norm(x) for x in actual)
    if isinstance(expected, dict) and isinstance(actual, dict):
        for k, want in expected.items():
            got = actual.get(k)
            if isinstance(want, bool):
                if want != (got is not None):
                    return False
            elif not values_match(want, got, rel_tolerance):
                return False
        return True
    if isinstance(expected, str) and isinstance(actual, str):
        return _norm(expected) == _norm(actual)
    return False


def _num(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def _norm(v: Any) -> str:
    return str(v).strip().casefold()


def _fmt(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, list):
        s = ", ".join(map(str, v)) or "(none)"
    elif isinstance(v, dict):  # tool maps: show installed or not, not the version strings
        s = ", ".join(f"{k}:{'no' if x in (None, False) else 'yes'}" for k, x in v.items())
    else:
        s = str(v)
    return s if len(s) <= 36 else s[:33] + "..."

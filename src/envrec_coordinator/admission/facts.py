"""The admission fact: one tested fact per combination (docs/ROUTER_API.md section 4), written by the exam and read by the router. Facts are appended to the coordinator's event log
(append-only, M1) as `admission_fact` events; the newest one for a key is the current fact. Nothing here decides a route."""

from __future__ import annotations

import statistics
from collections.abc import Callable
from dataclasses import dataclass, field

from envrec_coordinator.admission import strings as S
from envrec_coordinator.admission.suite import CODE_GRADERS, Suite

FACT_EVENT = "admission_fact"
ABORT_EVENT = "admission_aborted"


class WeakGraderRefused(ValueError):
    """A result that counts a task not graded by code was about to be stored as an admission (A1)."""


@dataclass(frozen=True)
class Combo:
    """What a measurement is valid for. The first five fields are the router's key; the last two are what invalidates a fact when they change (ROUTER_API 4)."""

    task_type: str
    machine_id: str
    environment_id: str
    model_id: str
    access_id: str
    model_sha256: str | None = None
    environment_version: str | None = None

    @property
    def key(self) -> str:
        return f"admission.{self.task_type}.{self.machine_id}.{self.environment_id}.{self.model_id}.{self.access_id}"


def identity_of(combo: Combo, suite_hash: str) -> dict:
    """What a measurement is valid for beyond the key (model file, environment version, suite). The router compares this with the same call made NOW; any difference makes the fact absent for it."""
    return {"model_sha256": combo.model_sha256, "environment_version": combo.environment_version, "suite_hash": suite_hash}


@dataclass(frozen=True)
class TaskResult:
    task_id: str
    passed: bool
    grader: str
    reason: str
    seconds: float
    reply_ref: str | None = None


@dataclass(frozen=True)
class ExamOutcome:
    combo: Combo
    suite_id: str
    suite_hash: str
    threshold: int
    of: int
    results: tuple[TaskResult, ...]
    early_stop: str | None  # None | "first_3" | "unreachable"
    stop_unreachable: bool = True

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed and r.grader in CODE_GRADERS)

    @property
    def admitted(self) -> bool:
        return self.early_stop is None and len(self.results) == self.of and self.passed >= self.threshold


def label_of(passed: int, of: int, threshold: int, ran: int, early_stop: str | None) -> str:
    if early_stop:
        return S.LABEL_STOPPED.format(passed=passed, of=of, ran=ran, why=S.STOP_FIRST_3 if early_stop == "first_3" else S.STOP_UNREACHABLE, threshold=threshold)
    if passed >= threshold:
        return S.LABEL_MEASURED.format(passed=passed, of=of)
    return S.LABEL_NOT_ADMITTED.format(passed=passed, of=of, threshold=threshold)


def fact_value(out: ExamOutcome, measured_at: float) -> dict:
    """The value of the fact. `passed` counts code-graded passes only; a weak-graded task would have to sit under `weak`, never here."""
    if any(r.grader not in CODE_GRADERS for r in out.results):
        raise WeakGraderRefused("a result with a task not graded by code cannot be stored as an admission")
    ran = len(out.results)
    return {"status": "tested", "identity": identity_of(out.combo, out.suite_hash), "passed": out.passed, "of": out.of, "ran": ran, "threshold": out.threshold, "suite_id": out.suite_id, "suite_hash": out.suite_hash, "tasks": [r.task_id for r in out.results],
            "graded_by": "code", "measured_at": measured_at, "early_stop": out.early_stop is not None, "stop_reason": out.early_stop, "stop_unreachable": out.stop_unreachable,
            "outcome": "admitted" if out.admitted else "not_admitted", "label": label_of(out.passed, out.of, out.threshold, ran, out.early_stop),
            "model_sha256": out.combo.model_sha256, "environment_version": out.combo.environment_version, "samples_per_task": 1,
            "results": [{"task": r.task_id, "passed": r.passed, "grader": r.grader, "reason": r.reason, "seconds": round(r.seconds, 2), "reply_ref": r.reply_ref} for r in out.results]}


@dataclass(frozen=True)
class AdmissionView:
    """What a reader (the router, the options list, the owner's chat) gets for one combination right now."""

    state: str  # "none" | "measured" | "not_admitted" | "stale"
    label: str
    admitted: bool = False  # a fresh fact whose passed >= its threshold
    stale: tuple[str, ...] = ()  # which of the combination's inputs changed since the measurement
    fact: dict = field(default_factory=dict)


def view_of(fact: dict | None, combo: Combo, suite: Suite | None, threshold: int) -> AdmissionView:
    """Read a stored fact against the CURRENT inputs. A mismatch is stale, shown as such, and never silently reused (spec 8.6)."""
    if not fact:
        return AdmissionView("none", S.LABEL_PROVISIONAL)
    changed = []
    if suite is None or fact.get("suite_hash") != suite.hash:
        changed.append("suite_hash")
    if fact.get("model_sha256") != combo.model_sha256:
        changed.append("model_sha256")
    if fact.get("environment_version") != combo.environment_version:
        changed.append("environment_version")
    if fact.get("threshold") != threshold:
        changed.append("threshold")
    if fact.get("graded_by") != "code":
        changed.append("grader")
    if changed:
        why = " and ".join(S.CHANGED.get(c, c) for c in changed)
        return AdmissionView("stale", S.LABEL_STALE.format(passed=fact.get("passed", 0), of=fact.get("of", 0), changed=why), False, tuple(changed), fact)
    admitted = fact["passed"] >= fact["threshold"] and not fact.get("early_stop")
    return AdmissionView("measured" if admitted else "not_admitted", fact["label"], admitted, (), fact)


def allows(sensitivity: str, view: AdmissionView) -> tuple[bool, str]:
    """The hook the router calls for a candidate. A combination measured below the mark is dropped for every task (spec 8.1). `confidential` goes only to a combination that is measured
    here, fresh, and admitted (spec 7.3); stale and unmeasured combinations are provisional."""
    if view.state == "not_admitted":
        return False, "not_admitted"
    if sensitivity == "confidential":
        return (True, "measured_here") if view.admitted else (False, "provisional_only")
    return True, "measured_here" if view.admitted else "provisional"


class AdmissionFacts:
    """Reads and writes admission facts in an `EventLog`."""

    def __init__(self, log, clock: Callable[[], float]) -> None:
        self.log, self.clock = log, clock

    def write(self, out: ExamOutcome) -> dict:
        value = fact_value(out, self.clock())
        self.log.add(FACT_EVENT, {"fact": out.combo.key, "status": "tested", "value": value, "tag": "GUARD"})
        return value

    def current(self, key: str) -> dict | None:
        rows = [e for e in self.log.events(FACT_EVENT) if e.payload.get("fact") == key]
        return rows[-1].payload["value"] if rows else None

    def view(self, combo: Combo, suite: Suite | None, threshold: int) -> AdmissionView:
        return view_of(self.current(combo.key), combo, suite, threshold)

    def aborted(self, combo: Combo, reason: str, ran: int) -> None:
        self.log.add(ABORT_EVENT, {"fact": combo.key, "reason": reason, "ran": ran, "tag": "GUARD"})


def measured_of(view: AdmissionView) -> dict | None:
    """The `measured` slot that `options.order_options` already reads: pass rate first, then time. Only a fresh measurement fills it."""
    if view.state not in ("measured", "not_admitted"):
        return None
    f = view.fact
    secs = [r["seconds"] for r in f.get("results", []) if r.get("seconds") is not None]
    return {"pass_rate": f["passed"] / f["of"], "seconds": statistics.median(secs) if secs else None, "label": f["label"], "passed": f["passed"], "of": f["of"], "suite_hash": f["suite_hash"]}

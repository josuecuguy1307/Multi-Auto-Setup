"""Empirical discovery: run the approved trials in order, measure each, keep the winner, remove the losers.

Hard constraints filter first and the owned score ranks (plan.py / score.py). Then bounded trials: every trial checks the
budgets BEFORE it starts (trial count, new download bytes, controller disk reserve, wall time) and the run stops at the first
one that would be broken. A trial is installed, verified by the real-model verifier (R4), measured, and removed again unless
it is the best so far. At the end only the winner is installed. The result is saved as a skill keyed by the device signature.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from envrec.contracts import Actor, Event
from envrec.discover.plan import ApprovedPlan, PlanGate, TrialPlan
from envrec.discover.skill import save_skill
from envrec.pairing.profile import DeviceProfile
from envrec.store import Store
from envrec.verify.model_check import ModelVerification

GIB = 2**30


class TrialRunner(Protocol):
    """What discovery needs from the world. The real one downloads (cached), deploys through the Provisioner and verifies."""

    def prepare(self, entry_id: str) -> int:
        """Fetch the entry's artifacts into the controller cache. Returns the bytes actually downloaded."""
    def deploy_and_verify(self, entry_id: str) -> ModelVerification: ...
    def undo(self) -> tuple[bool, str]: ...


@dataclass
class TrialOutcome:
    entry_id: str
    status: str  # verified | failed | error | not_started
    tokens_per_s: float | None = None
    rss_mib: float | None = None
    startup_s: float | None = None
    failed: list[str] = field(default_factory=list)
    downloaded_bytes: int = 0
    wall_s: float = 0.0
    note: str = ""


@dataclass
class DiscoveryResult:
    ok: bool
    winner: str | None
    outcomes: list[TrialOutcome]
    stopped: str  # "" when every planned trial ran, else which budget (or failure) ended the run
    plan_digest: str
    skill_id: str | None = None
    installed: str | None = None  # the entry left installed on the device, if any
    downloaded_bytes: int = 0
    notes: list[str] = field(default_factory=list)


def better(a: TrialOutcome, b: TrialOutcome | None) -> bool:
    """Higher tokens/s wins; with no throughput figure a verified trial still beats a failed one."""
    if b is None:
        return True
    return (a.tokens_per_s or 0.0) > (b.tokens_per_s or 0.0)


def discover(plan_approval: ApprovedPlan, p: DeviceProfile, runner: TrialRunner, store: Store, *,
             free_fn: Callable[[], int], clock: Callable[[], float] = time.monotonic, gate: PlanGate | None = None) -> DiscoveryResult:
    plan: TrialPlan = plan_approval.consume()  # raises if this approval was used already or the plan changed
    b = plan.budgets
    start, downloaded = clock(), 0
    outcomes: list[TrialOutcome] = []
    best: TrialOutcome | None = None
    installed: str | None = None
    res = DiscoveryResult(False, None, outcomes, "", plan.digest)

    def log(kind: str, payload: dict) -> None:
        store.append_event(Event(actor=Actor.policy, type=kind, payload={"target": p.target_id, "plan": plan.digest, **payload}))

    def remove_installed() -> bool:
        nonlocal installed
        if installed is None:
            return True
        ok, msg = runner.undo()
        log("discovery_undo", {"entry": installed, "ok": ok, "message": msg})
        if not ok:
            res.stopped, res.notes = "undo_failed", res.notes + [f"could not remove {installed}: {msg}"]
            return False
        installed = None
        return True

    try:
        for i, t in enumerate(plan.trials):
            if i >= b.max_trials:
                res.stopped = "max_trials"
                break
            if clock() - start >= b.wall_time_s:
                res.stopped = "wall_time"
                break
            if (downloaded + t.new_download_bytes) / 1e9 > b.max_download_gb:
                res.stopped = "download_budget"
                break
            if free_fn() - t.new_download_bytes < b.disk_reserve_gib * GIB:
                res.stopped = "disk_reserve"
                break
            if not remove_installed():
                break
            out = TrialOutcome(t.entry_id, "not_started")
            t0 = clock()
            try:
                out.downloaded_bytes = runner.prepare(t.entry_id)
                downloaded += out.downloaded_bytes
                installed = t.entry_id
                v = runner.deploy_and_verify(t.entry_id)
                out.tokens_per_s, out.rss_mib, out.startup_s = v.tokens_per_s, v.rss_mib, v.startup_s
                out.status, out.failed, out.note = ("verified" if v.ok else "failed"), v.failed, v.reason
            except Exception as exc:  # a trial that errors is a recorded result, not a crash of the run
                out.status, out.note = "error", f"{type(exc).__name__}: {str(exc)[:160]}"
            out.wall_s = round(clock() - t0, 2)
            outcomes.append(out)
            log("discovery_trial", {k: v for k, v in out.__dict__.items()})
            if out.status == "verified" and better(out, best):
                best = out
            elif installed == t.entry_id:
                if not remove_installed():  # a loser is removed at once
                    break
        res.downloaded_bytes = downloaded

        if res.stopped == "undo_failed":
            return res
        if best is None:
            remove_installed()
            res.notes.append("no trial produced a verified server")
            log("discovery_done", {"winner": None, "stopped": res.stopped})
            return res
        # at this point `installed` is the latest verified best, or a worse earlier one was already removed
        if installed != best.entry_id:
            if not remove_installed():
                return res
            try:
                v = runner.deploy_and_verify(best.entry_id)  # artifacts are cached: no new download
            except Exception as exc:
                res.notes.append(f"re-installing the winner failed: {type(exc).__name__}: {exc}")
                remove_installed()
                return res
            if not v.ok:
                res.notes.append(f"the winner did not verify again: {v.reason}")
                runner.undo()
                return res
            installed = best.entry_id
        res.ok, res.winner, res.installed = True, best.entry_id, installed
        res.skill_id = save_skill(store, p, plan, res)
        log("discovery_done", {"winner": best.entry_id, "stopped": res.stopped, "skill": res.skill_id})
        return res
    finally:
        if gate is not None:
            gate.close()

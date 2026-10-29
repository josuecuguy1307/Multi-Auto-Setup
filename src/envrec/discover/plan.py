"""The trial plan: what a discovery run will download and put on one device, approved ONCE as an exact object.

The approval is bound to the plan's digest. It covers only the actions the plan lists (this target, the listed
artifacts by sha256, paths under the install root), it can be used for one run, and it ends with that run. There is no
standing approval: a changed plan has a different digest and needs a new yes. Anything outside the plan still asks.
"""

from __future__ import annotations

import hashlib
import json
import shlex
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

from envrec.contracts import Actor, Event
from envrec.discover.score import top_k
from envrec.pairing.profile import DeviceProfile
from envrec.pairing.rules import feasible
from envrec.remote.approval import PendingAction
from envrec.remote.templates import INSTALL_ROOT
from envrec.store import Store

GIB = 2**30
COVERED_RECIPES = {"install_runtime", "transfer_model", "start_server", "uninstall", "fetch_artifact"}


@dataclass(frozen=True)
class Budgets:
    max_trials: int = 3
    max_download_gb: float = 4.0  # total new bytes pulled from the internet in this run
    disk_reserve_gib: float = 4.0  # the controller must keep at least this free after any download
    wall_time_s: float = 1800.0


@dataclass(frozen=True)
class PlannedTrial:
    entry_id: str
    score: float
    runtime_file: str
    runtime_url: str
    runtime_sha256: str
    runtime_bytes: int
    model_file: str
    model_url: str
    model_sha256: str
    model_bytes: int
    new_download_bytes: int  # bytes this trial adds, counting a shared runtime or model once


@dataclass(frozen=True)
class TrialPlan:
    target_id: str
    signature: dict
    trials: tuple[PlannedTrial, ...]
    budgets: Budgets
    worker_sha256: str = ""

    @property
    def digest(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()

    @property
    def allowed_shas(self) -> set[str]:
        out = {t.runtime_sha256 for t in self.trials} | {t.model_sha256 for t in self.trials}
        return out | ({self.worker_sha256} if self.worker_sha256 else set())

    @property
    def total_download_bytes(self) -> int:
        return sum(t.new_download_bytes for t in self.trials)

    def render(self) -> str:
        b = self.budgets
        lines = [f"=== DISCOVERY PLAN {self.digest[:12]} (approve once; covers exactly this) ===", f"target: {self.target_id}  signature: {self.signature}",
                 f"budgets: up to {b.max_trials} trials, {b.max_download_gb:g} GB of new downloads, {b.disk_reserve_gib:g} GiB kept free on the controller, "
                 f"{b.wall_time_s / 60:g} minutes", "trials (each is installed, measured, then removed unless it wins):"]
        for i, t in enumerate(self.trials, 1):
            lines.append(f"  {i}. {t.entry_id}  score {t.score}\n     runtime {t.runtime_file} {t.runtime_bytes / 1e6:.0f} MB sha256 {t.runtime_sha256[:16]}...\n"
                         f"     model   {t.model_file} {t.model_bytes / 1e6:.0f} MB sha256 {t.model_sha256[:16]}...  (new download {t.new_download_bytes / 1e6:.0f} MB)")
        lines += [f"total new download: {self.total_download_bytes / 1e9:.2f} GB",
                  "Losing trials are uninstalled. Only the winner stays. Writes outside this plan still ask separately.",
                  "Type 'approve' to authorise exactly this plan once, anything else denies."]
        return "\n".join(lines)


def build_plan(cat, p: DeviceProfile, budgets: Budgets = Budgets(), *, k: int = 3, worker_sha256: str = "",
               cached: Callable[[str], bool] = lambda artifact_id: False) -> TrialPlan:
    """Filter by hard constraints, rank by the owned score, take top-K, then cut to the trial and download budgets.
    `cached(artifact_id)` says an artifact is already in the controller cache and costs no new download."""
    fit = feasible(cat, p).entries
    chosen, seen, total = [], set(), 0
    for e, s in top_k(fit, p, len(fit)):  # the whole ranking: a candidate that breaks the budget is skipped, the next one is tried
        if len(chosen) >= min(k, budgets.max_trials):
            break
        new = 0
        for aid, size in ((e.runtime.id, e.runtime.bytes), (e.model.id, e.model.bytes)):
            if aid not in seen and not cached(aid):
                new += size
        if (total + new) / 1e9 > budgets.max_download_gb:
            continue  # skipping this one does not stop a cheaper candidate further down from fitting
        seen |= {e.runtime.id, e.model.id}
        total += new
        chosen.append(PlannedTrial(e.id, s, e.runtime.file, e.runtime.url, e.runtime.sha256, e.runtime.bytes, e.model.file, e.model.url,
                                   e.model.sha256, e.model.bytes, new))
    return TrialPlan(p.target_id, p.signature, tuple(chosen), budgets, worker_sha256)


class PlanNotApproved(PermissionError):
    pass


@dataclass
class ApprovedPlan:
    plan: TrialPlan
    digest: str
    used: bool = False

    def consume(self) -> TrialPlan:
        if self.used:
            raise PlanNotApproved("this plan approval was already used; approvals are not standing")
        if self.plan.digest != self.digest:
            raise PlanNotApproved("the plan changed after it was approved")
        self.used = True
        return self.plan


def request_plan_approval(plan: TrialPlan, store: Store, ask: Callable[[str], str], show: Callable[[str], None]) -> ApprovedPlan | None:
    show(plan.render())
    granted = ask("> ").strip().lower() == "approve"
    store.append_event(Event(actor=Actor.human, type="plan_approval", payload={
        "target": plan.target_id, "digest": plan.digest, "granted": granted, "trials": [t.entry_id for t in plan.trials]}))
    return ApprovedPlan(plan, plan.digest) if granted else None


class PlanGate:
    """Stands in for the ApprovalGate during one approved run. It auto-grants only what the plan lists; all else asks the human."""

    def __init__(self, inner, approved: ApprovedPlan) -> None:
        self.inner, self.plan, self.approved = inner, approved.plan, approved
        self.store, self.ask, self.show = inner.store, inner.ask, inner.show
        self.auto_granted = 0
        self.live = True

    @property
    def asked(self) -> int:
        return self.inner.asked

    def close(self) -> None:
        self.live = False  # the approval ends with the run

    def covers(self, a: PendingAction) -> bool:
        if not self.live or not self.approved.used or a.recipe not in COVERED_RECIPES:
            return False
        if a.recipe == "fetch_artifact":
            return a.checksum in self.plan.allowed_shas
        if a.target_id != self.plan.target_id:
            return False
        if a.checksum is not None and a.checksum not in self.plan.allowed_shas:
            return False
        paths = [t for t in shlex.split(a.command.replace(" ; ", " ")) if t.startswith("/") or t.startswith("of=/")]
        return all(t.removeprefix("of=").startswith(f"{INSTALL_ROOT}/worker") or t == INSTALL_ROOT for t in paths)

    def request(self, a: PendingAction) -> bool:
        if self.covers(a):
            self.auto_granted += 1
            self.store.append_event(Event(actor=Actor.policy, type="approval", payload={
                "recipe": a.recipe, "target": a.target_id, "origin": a.origin.value, "command": a.command, "checksum": a.checksum,
                "artifact": a.artifact, "granted": True, "via_plan": self.plan.digest}))
            return True
        return self.inner.request(a)

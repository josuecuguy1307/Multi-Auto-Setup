"""R3b empirical discovery: ranking, bounded trials, one-time plan approval, loser removal, device-signature skills."""

from __future__ import annotations

import pytest

from envrec.discover import (ApprovedPlan, Budgets, PlanGate, PlanNotApproved, build_plan, discover, find_skill, request_plan_approval,
                             reverify_here, score, skill_status_here, top_k)
from envrec.contracts import Origin
from envrec.pairing import DeviceProfile, feasible
from envrec.remote.approval import ApprovalGate, PendingAction
from envrec.store import Store
from envrec.verify.model_check import ModelVerification
from test_pairing import catalog, md, prof, rt

GB = 10**9


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "s") as s:
        yield s


def cat():
    return catalog([rt("arm64")], [md(0.5), md(1.5), md(3), md(3, "Q8_0"), md(4)])


def B(**kw):
    return Budgets(**{"max_download_gb": 50.0, **kw})


def make_plan(p=None, **kw):
    kw.setdefault("budgets", B())
    return build_plan(cat(), p or prof(8), **kw)


def approved(plan, store, answer="approve"):
    return request_plan_approval(plan, store, lambda _: answer, lambda _: None)


class FakeRunner:
    def __init__(self, tps=None, fail=(), boom=(), undo_ok=True):
        self.tps, self.fail, self.boom, self.undo_ok = tps or {}, set(fail), set(boom), undo_ok
        self.ops, self.installed = [], None

    def prepare(self, entry_id):
        self.ops.append(("prepare", entry_id))
        return 100_000_000

    def deploy_and_verify(self, entry_id):
        self.ops.append(("deploy", entry_id))
        if entry_id in self.boom:
            raise RuntimeError("disk full on the device")
        self.installed = entry_id
        if entry_id in self.fail:
            return ModelVerification(False, ["identity"], {"identity": "wrong model"})
        return ModelVerification(True, [], tokens_per_s=self.tps.get(entry_id, 10.0), rss_mib=500.0, startup_s=2.0)

    def undo(self):
        self.ops.append(("undo", self.installed))
        ok = self.undo_ok
        if ok:
            self.installed = None
        return ok, "removed" if ok else "device unreachable"


def run(plan, store, runner, p=None, **kw):
    ap = approved(plan, store)
    return discover(ap, p or prof(8), runner, store, free_fn=kw.pop("free_fn", lambda: 500 * GB), **kw)


# ---- ranking ------------------------------------------------------------------------------------------------------

def test_hard_constraints_filter_before_the_score_ranks():
    p = prof(2.0)
    entries = feasible(cat(), p).entries
    assert all(e.model.params_b <= 1.5 for e in entries)  # 3B and 4B never reach the ranking on a 2 GiB device
    assert [e.model.params_b for e, _ in top_k(entries, p, 3)] == [1.5, 0.5]


def test_score_is_deterministic_and_larger_models_score_higher_when_they_fit():
    p = prof(16)
    by = {e.id: score(e, p) for e in feasible(cat(), p).entries}
    assert by == {e.id: score(e, p) for e in feasible(cat(), p).entries}
    q4 = {k: v for k, v in by.items() if k.endswith("Q4_K_M.gguf")}
    assert sorted(q4, key=q4.get, reverse=True)[0].count("m4b") == 1 and q4[[k for k in q4 if "m3b" in k][0]] > q4[[k for k in q4 if "m1.5b" in k][0]]


def test_tight_ram_headroom_is_penalised():
    p = DeviceProfile("x", "linux", "arm64", 3.0, 50, "none")  # scored directly: the hard filter would reject this pair here
    e3 = next(e for e in cat().entries() if e.model.params_b == 3 and e.model.quantization == "Q4_K_M")
    roomy = DeviceProfile("x", "linux", "arm64", 32, 50, "none")
    assert score(e3, p) < score(e3, roomy)


def test_top_k_default_is_three_and_a_model_appears_at_most_twice():
    got = top_k(feasible(cat(), prof(16)).entries, prof(16))
    assert len(got) == 3
    from collections import Counter
    assert max(Counter(e.model.repo for e, _ in got).values()) <= 2


# ---- the plan and its one-time approval -----------------------------------------------------------------------------

def test_plan_lists_exact_artifacts_and_counts_shared_downloads_once():
    plan = make_plan()
    assert len(plan.trials) == 3
    first, *rest = plan.trials
    assert first.new_download_bytes == first.runtime_bytes + first.model_bytes  # the shared runtime is downloaded once, with the first trial...
    assert [t.new_download_bytes for t in rest] == [t.model_bytes for t in rest]  # ...and is not counted again for the others
    ids = [t.entry_id for t in plan.trials]
    assert len(ids) == len(set(ids))
    # the runtime is shared by every trial, so it is paid for in the first trial only
    assert plan.total_download_bytes < sum(t.runtime_bytes + t.model_bytes for t in plan.trials)
    text = plan.render()
    for t in plan.trials:
        assert t.runtime_sha256[:16] in text and t.model_sha256[:16] in text and t.entry_id in text


def test_plan_respects_budgets_trials_and_download():
    assert len(make_plan(budgets=B(max_trials=1)).trials) == 1
    tiny = make_plan(budgets=Budgets(max_download_gb=0.5))
    assert tiny.total_download_bytes <= 0.5e9 and len(tiny.trials) >= 1
    assert make_plan(budgets=Budgets(max_download_gb=0.001)).trials == ()


def test_cached_artifacts_cost_no_new_download():
    plan = make_plan(cached=lambda aid: True)
    assert plan.total_download_bytes == 0


def test_digest_changes_when_anything_changes():
    a, b = make_plan(), make_plan(budgets=B(max_trials=2))
    assert a.digest != b.digest and a.digest == make_plan().digest


def test_denied_plan_is_not_approved_and_logged(store):
    assert approved(make_plan(), store, "no") is None
    assert store.events(type="plan_approval")[0].payload["granted"] is False


def test_an_approval_is_good_for_one_run_only(store):
    plan, runner = make_plan(), FakeRunner()
    ap = approved(plan, store)
    discover(ap, prof(8), runner, store, free_fn=lambda: 500 * GB)
    with pytest.raises(PlanNotApproved, match="already used"):
        discover(ap, prof(8), runner, store, free_fn=lambda: 500 * GB)


def test_a_plan_changed_after_approval_is_refused(store):
    plan = make_plan()
    ap = approved(plan, store)
    ap.plan = make_plan(budgets=B(max_trials=1))  # same digest recorded, different content
    with pytest.raises(PlanNotApproved, match="changed"):
        discover(ap, prof(8), FakeRunner(), store, free_fn=lambda: 500 * GB)


# ---- the gate that honours the plan ---------------------------------------------------------------------------------------

class Human:
    def __init__(self, answer="deny"):
        self.answer, self.prompts = answer, []

    def ask(self, prompt):
        self.prompts.append(prompt)
        return self.answer

    def show(self, text):
        pass


def gate_for(plan, store, answer="deny", used=True):
    h = Human(answer)
    ap = ApprovedPlan(plan, plan.digest, used=used)
    return PlanGate(ApprovalGate(store, h.ask, h.show), ap), h


def action(recipe="transfer_model", cmd="dd of=/opt/envrec/worker/models/m.gguf status=none", target="dev", sha=None):
    return PendingAction(recipe, cmd, target, Origin.builtin, sha)


def test_the_gate_grants_what_the_plan_lists_without_asking(store):
    plan = make_plan(worker_sha256="w" * 64)
    g, h = gate_for(plan, store)
    sha = plan.trials[0].model_sha256
    assert g.request(action(sha=sha)) and g.request(action("start_server", "sh -c x sh /opt/envrec/worker/w.pyz")) and h.prompts == []
    assert g.request(action("fetch_artifact", "GET https://x/f", "controller", sha=plan.trials[0].runtime_sha256))
    assert g.auto_granted == 3
    assert all(e.payload.get("via_plan") == plan.digest for e in store.events(type="approval"))


@pytest.mark.parametrize("a", [
    action(sha="f" * 64),                                              # a file the plan does not list
    action(target="someone-elses-box"),                                # another device
    action(cmd="dd of=/etc/passwd status=none"),                       # a path outside the install root
    action(cmd="rm -rf /opt/envrec/worker /home/owner"),               # a path outside, smuggled in with a legit one
    action("open_tunnel", "ssh -L 1:2:3 host", "dev"),                 # a recipe the plan never covered
    action("fetch_artifact", "GET https://x/other", "controller", sha="e" * 64),
])
def test_everything_else_still_asks_the_human(store, a):
    g, h = gate_for(make_plan(worker_sha256="w" * 64), store)
    assert g.request(a) is False and len(h.prompts) == 1 and g.auto_granted == 0


def test_an_unused_or_finished_approval_covers_nothing(store):
    plan = make_plan()
    g, h = gate_for(plan, store, used=False)
    assert g.request(action(sha=plan.trials[0].model_sha256)) is False
    g2, h2 = gate_for(plan, store)
    g2.close()
    assert g2.request(action(sha=plan.trials[0].model_sha256)) is False and len(h2.prompts) == 1


# ---- the run ----------------------------------------------------------------------------------------------------------------

def best_first(plan, tps):
    return {t.entry_id: v for t, v in zip(plan.trials, tps)}


def test_winner_stays_and_every_loser_is_removed(store):
    plan = make_plan()
    r = FakeRunner(tps=best_first(plan, [20.0, 35.0, 12.0]))
    out = run(plan, store, r)
    assert out.ok and out.winner == plan.trials[1].entry_id and out.stopped == "" and out.installed == out.winner
    assert r.installed == out.winner  # exactly one thing left on the device
    deploys = [o[1] for o in r.ops if o[0] == "deploy"]
    assert deploys == [t.entry_id for t in plan.trials] + [out.winner]  # three trials, then the winner put back (artifacts cached)
    assert r.ops.count(("undo", plan.trials[0].entry_id)) == 1


def test_no_reinstall_when_the_last_trial_is_the_winner(store):
    plan = make_plan()
    r = FakeRunner(tps=best_first(plan, [5.0, 6.0, 30.0]))
    out = run(plan, store, r)
    assert out.winner == plan.trials[2].entry_id and [o for o in r.ops if o[0] == "deploy"].__len__() == 3


def test_failed_and_erroring_trials_are_recorded_and_never_win(store):
    plan = make_plan()
    ids = [t.entry_id for t in plan.trials]
    r = FakeRunner(tps=best_first(plan, [10.0, 99.0, 8.0]), fail=[ids[1]], boom=[ids[0]])
    out = run(plan, store, r)
    by = {o.entry_id: o for o in out.outcomes}
    assert by[ids[0]].status == "error" and "disk full" in by[ids[0]].note
    assert by[ids[1]].status == "failed" and by[ids[1]].failed == ["identity"]
    assert out.winner == ids[2] and r.installed == ids[2]


def test_nothing_verified_means_no_winner_and_nothing_left_installed(store):
    plan = make_plan()
    r = FakeRunner(fail=[t.entry_id for t in plan.trials])
    out = run(plan, store, r)
    assert not out.ok and out.winner is None and r.installed is None and out.skill_id is None
    assert find_skill(store, prof(8)) is None


@pytest.mark.parametrize(("budgets", "free", "reason", "ran"), [
    (B(max_trials=2), 500 * GB, "", 2),
    (B(wall_time_s=15), 500 * GB, "wall_time", 1),
    (B(disk_reserve_gib=4), int(4.1 * 2**30), "disk_reserve", 0),
])
def test_each_budget_stops_the_run_and_says_which(store, budgets, free, reason, ran):
    plan = make_plan(budgets=budgets)
    ticks = iter(range(0, 1000, 10))  # every clock read is 10 s later
    r = FakeRunner()
    out = run(plan, store, r, free_fn=lambda: free, clock=lambda: float(next(ticks)))
    assert out.stopped == reason and len(out.outcomes) == ran
    assert r.installed in (None, out.winner)


def test_the_download_budget_is_checked_before_each_trial(store):
    plan = make_plan()
    tight = TrialPlanWithBudget(plan, Budgets(max_download_gb=plan.trials[0].new_download_bytes / 1e9 + 0.001))
    r = FakeRunner()
    ap = approved(tight, store)
    out = discover(ap, prof(8), r, store, free_fn=lambda: 500 * GB)
    assert out.stopped == "download_budget" and len(out.outcomes) == 1 and out.downloaded_bytes == 100_000_000


def TrialPlanWithBudget(plan, budgets):
    import dataclasses
    return dataclasses.replace(plan, budgets=budgets)


def test_a_failed_undo_stops_the_run_instead_of_piling_up_installs(store):
    plan = make_plan()
    r = FakeRunner(undo_ok=False)
    out = run(plan, store, r)
    assert not out.ok and out.stopped == "undo_failed" and len(out.outcomes) == 1


def test_events_record_every_trial_and_the_result(store):
    plan = make_plan()
    run(plan, store, FakeRunner(tps=best_first(plan, [1.0, 2.0, 3.0])))
    assert len(store.events(type="discovery_trial")) == 3 and store.events(type="discovery_done")[0].payload["winner"] == plan.trials[2].entry_id
    assert all(e.payload["plan"] == plan.digest for e in store.events(type="discovery_trial"))


# ---- the saved result: keyed by device signature, re-verified elsewhere -----------------------------------------------------------

def test_result_is_saved_keyed_by_signature_and_found_for_the_same_signature_only(store):
    plan = make_plan()
    out = run(plan, store, FakeRunner(tps=best_first(plan, [1.0, 9.0, 3.0])))
    sk = find_skill(store, prof(8))
    assert sk and sk["entry"] == out.winner and sk["signature"] == prof(8).signature and sk["tokens_per_s"] == 9.0
    assert find_skill(store, prof(8, arch="amd64")) is None  # different arch
    assert find_skill(store, prof(16)) is None  # different RAM bracket
    assert find_skill(store, prof(8, accel="nvidia")) is None  # different accelerator


def test_on_a_new_device_the_skill_is_unverified_until_it_passes_there(store):
    plan = make_plan()
    run(plan, store, FakeRunner(tps=best_first(plan, [1.0, 9.0, 3.0])))
    sk = find_skill(store, prof(8))
    other = DeviceProfile("other-box", "linux", "arm64", 8.0, 30, "none", 4)
    assert other.signature == prof(8).signature
    assert skill_status_here(store, sk, "dev") == "verified_here"          # measured there
    assert skill_status_here(store, sk, "other-box") == "unverified_here"  # same signature, never tried here

    ok, msg = reverify_here(store, sk, other, lambda entry: ModelVerification(False, ["identity"], {"identity": "x"}))
    assert not ok and skill_status_here(store, sk, "other-box") == "unverified_here"
    ok, msg = reverify_here(store, sk, other, lambda entry: ModelVerification(True, [], tokens_per_s=7.0))
    assert ok and skill_status_here(store, sk, "other-box") == "verified_here"


def test_a_skill_cannot_be_reverified_on_a_device_with_another_signature(store):
    plan = make_plan()
    run(plan, store, FakeRunner())
    sk = find_skill(store, prof(8))
    other = DeviceProfile("arm-big", "linux", "arm64", 32.0, 30, "none", 4)
    ok, msg = reverify_here(store, sk, other, lambda entry: ModelVerification(True, []))
    assert not ok and "signature" in msg

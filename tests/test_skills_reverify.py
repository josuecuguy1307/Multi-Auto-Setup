import json
from pathlib import Path

import pytest

import test_rung45 as base
from envrec.contracts import FactStatus
from envrec.ladder.generate import ReplayRunner
from envrec.scoring import load_ground_truth, score_target
from envrec.skills import skill_status_here
from envrec.store import Store

ROOT = Path(__file__).resolve().parent.parent
SIG = "usb:ffff:0002"


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "s") as s:
        yield s


def learn_on_the_controller_device(store):
    t, _, _ = base.drive(store, base.STORY + [base.propose(), base.DONE], base.ScriptedVerifier([base.PASS]))
    skill = store.find_skill(SIG)
    assert skill and skill_status_here(store, skill, t.id) == "verified_here"  # verified on the device it was learned on
    return t, skill


def test_a_skill_is_unverified_here_on_any_other_device_until_its_tests_pass_there(store):
    first, skill = learn_on_the_controller_device(store)
    from envrec.contracts import Target, TargetKind
    other = store.put_target(Target(kind=TargetKind.usb, ring=1, handle="replay:zxq-sensor-b"))
    assert skill_status_here(store, skill, other.id) == "unverified_here"
    assert skill_status_here(store, skill, first.id) == "verified_here"  # verifying elsewhere does not change the original


def test_on_a_second_unit_the_tests_run_again_before_the_facts_become_verified(store, monkeypatch):
    learn_on_the_controller_device(store)
    runs = []
    real = ReplayRunner.run
    monkeypatch.setattr(ReplayRunner, "run", lambda self, code, chunk: (runs.append(chunk), real(self, code, chunk))[1])
    t2, s2, model = base.drive(store, [base.call("read_usb_descriptor"), base.call("try_known_skill"), base.DONE], name="zxq-sensor-b")
    f = base.facts(store, t2)
    assert f["sensor.temperature_c"].value == 21 and f["sensor.humidity"].value == 55  # b's own readings, not the original's
    assert all(f[k].status == FactStatus.verified for k in ("sensor.temperature_c", "sensor.humidity"))
    assert f["sensor.humidity"].evidence[-1].provider == "skill-reverify"
    assert len(runs) >= 4  # three chunks of its stored test on this device, then the real use
    assert any("~~ZXQ 21/55" in c for c in runs)  # they ran on THIS device's data
    assert skill_status_here(store, store.find_skill(SIG), t2.id) == "verified_here"
    report = score_target(store, t2.id, load_ground_truth(ROOT / "ground_truth" / "replay-zxq-sensor-b.yaml"))
    assert {r.outcome for r in report.rows} == {"correct"}
    types = [e.type for e in store.events(run_id=s2.run_id)]
    assert "research" not in types and "probe_proposed" not in types


def test_once_verified_here_the_tests_are_not_re_run_on_the_same_device(store, monkeypatch):
    learn_on_the_controller_device(store)
    base.drive(store, [base.call("read_usb_descriptor"), base.call("try_known_skill"), base.DONE], name="zxq-sensor-b")
    runs = []
    real = ReplayRunner.run
    monkeypatch.setattr(ReplayRunner, "run", lambda self, code, chunk: (runs.append(chunk), real(self, code, chunk))[1])
    from envrec.contracts import Target, TargetKind
    b = next(t for t in store.list_targets() if t.handle == "replay:zxq-sensor-b")
    from envrec.ladder import LadderAdapter, load_devices
    from envrec.adapters import ToolContext
    import asyncio
    adapter = LadderAdapter(load_devices(ROOT / "fixtures" / "replay"), base.USB, ReplayRunner())
    res = asyncio.run(adapter.run("try_known_skill", {}, ToolContext(store, store.get_target(b.id), None)))
    assert res.ok and len(runs) == 1  # already verified on this device: just the one real run


def test_a_lookalike_device_with_the_same_usb_ids_fails_the_tests_and_is_never_trusted(store):
    learn_on_the_controller_device(store)
    t2, s2, _ = base.drive(store, [base.call("read_usb_descriptor"), base.call("try_known_skill"), base.DONE], name="zxq-v2")
    f = base.facts(store, t2)
    assert f["skill.match"].value is False
    assert "unverified_here" in " ".join(f["skill.match"].evidence[0].limitations)
    assert "sensor.humidity" not in f and "sensor.temperature_c" not in f  # nothing from the skill was recorded
    assert skill_status_here(store, store.find_skill(SIG), t2.id) == "unverified_here"
    assert not [e for e in store.events(type="skill_verified_here") if e.payload["target"] == t2.id]
    assert store.find_skill(SIG).verified_runs == 1  # a failed reverification is not a successful run


def test_the_original_skill_is_untouched_by_a_failed_reverification(store):
    _, before = learn_on_the_controller_device(store)
    base.drive(store, [base.call("read_usb_descriptor"), base.call("try_known_skill"), base.DONE], name="zxq-v2")
    after = store.find_skill(SIG)
    assert (after.code_ref, after.tests_ref, after.signature) == (before.code_ref, before.tests_ref, before.signature)

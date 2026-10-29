import sqlite3

import pytest

from envrec.contracts import (
    Actor,
    Event,
    Evidence,
    Fact,
    FactStatus,
    Hypothesis,
    HypothesisOutcome,
    Skill,
    Target,
    TargetKind,
)
from envrec.store import RawHashMismatch, Store


def ev(provider="builtin", probe="p"):
    return Evidence(provider=provider, probe=probe, confidence=0.9)


def fact(target_id, key, value, status=FactStatus.tested, providers=("builtin",)):
    evidence = [ev(p) for p in providers] if status != FactStatus.unknown else []
    if status == FactStatus.hypothesis:
        evidence = [ev(p) for p in providers]
    return Fact(target_id=target_id, key=key, value=value, status=status, evidence=evidence)


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "s") as s:
        yield s


def test_everything_persists_across_reopen(tmp_path):
    root = tmp_path / "s"
    with Store(root) as s:
        t = s.put_target(Target(kind=TargetKind.host, ring=0, handle="this-machine"))
        s.write_fact(fact(t.id, "os.name", "macOS"))
        s.append_event(Event(actor=Actor.loop, type="run_start", run_id="r1"))
        h = s.put_hypothesis(Hypothesis(target_id=t.id, claim="a guess", rung=3))
        s.put_skill(Skill(signature="sig", code_ref="a", tests_ref="b"))
        ref = s.put_raw("raw output")
    with Store(root) as s:
        assert [x.handle for x in s.list_targets()] == ["this-machine"]
        assert s.current_fact(t.id, "os.name").value == "macOS"
        assert [e.type for e in s.events(run_id="r1")] == ["run_start"]
        assert s.hypotheses(t.id)[0].id == h.id
        assert s.find_skill("sig") is not None
        assert s.read_raw(ref) == b"raw output"


def test_supersede_and_contradiction_go_through_the_store(store):
    t = store.put_target(Target(kind=TargetKind.serial, ring=1, handle="replay:a"))
    hyp = store.write_fact(fact(t.id, "serial.protocol", "nmea", FactStatus.hypothesis, ("signatures",)))
    now = store.write_fact(fact(t.id, "serial.protocol", "scpi", FactStatus.tested, ("replay",)))
    assert now.status == FactStatus.contradicted
    assert len(now.evidence) == 2 and now.alt_values == ["scpi"]
    assert [f.id for f in store.fact_chain(now.id)] == [now.id, hyp.id]
    assert len(store.current_facts(t.id)) == 1  # only the newest of the chain is current
    assert store.get_fact(hyp.id).id == hyp.id  # the old fact is still readable


def test_hypothesis_confirmed_becomes_tested_not_verified(store):
    t = store.put_target(Target(kind=TargetKind.usb, ring=1, handle="replay:b"))
    store.write_fact(fact(t.id, "usb.vendor_name", "QinHeng", FactStatus.hypothesis, ("usb.ids",)))
    now = store.write_fact(fact(t.id, "usb.vendor_name", "QinHeng", FactStatus.tested, ("replay",)))
    assert now.status == FactStatus.tested


def test_unchanged_fact_writes_nothing(store):
    t = store.put_target(Target(kind=TargetKind.host, ring=0, handle="h"))
    first = store.write_fact(fact(t.id, "hw.ram", 16))
    again = store.write_fact(fact(t.id, "hw.ram", 16))
    assert again.id == first.id
    assert len(store.fact_chain(first.id)) == 1


def test_raw_hash_mismatch_is_detected(store):
    ref = store.put_raw(b"original")
    (store.root / ref.path).write_bytes(b"tampered")
    with pytest.raises(RawHashMismatch):
        store.read_raw(ref)


def test_raw_is_deduplicated_by_hash(store):
    a, b = store.put_raw("same"), store.put_raw(b"same")
    assert a == b


def test_events_cannot_be_edited_or_deleted(store):
    e = store.append_event(Event(actor=Actor.policy, type="decision"))
    with pytest.raises(sqlite3.IntegrityError):
        store.db.execute("UPDATE events SET type = 'x' WHERE id = ?", (e.id,))
    with pytest.raises(sqlite3.IntegrityError):
        store.db.execute("DELETE FROM events WHERE id = ?", (e.id,))
    assert [x.type for x in store.events()] == ["decision"]


def test_events_come_back_in_order(store):
    for i in range(5):
        store.append_event(Event(actor=Actor.loop, type=f"t{i}"))
    assert [e.type for e in store.events()] == [f"t{i}" for i in range(5)]


def test_authorization_only_comes_from_config(store):
    with pytest.raises(PermissionError):
        store.put_target(Target(kind=TargetKind.net_host, ring=2, handle="pi", authorized=True))
    t = store.put_target(Target(kind=TargetKind.net_host, ring=2, handle="pi", authorized=True),
                         from_config=True)
    assert store.get_target(t.id).authorized is True
    # discovery re-registering the same target cannot revoke what the config granted
    again = store.put_target(t.model_copy(update={"authorized": False}))
    assert again.authorized is True


def test_hypothesis_outcome_can_be_resolved(store):
    h = store.put_hypothesis(Hypothesis(target_id="t", claim="c", rung=3))
    done = store.resolve_hypothesis(h.id, HypothesisOutcome.confirmed)
    assert done.outcome == HypothesisOutcome.confirmed
    assert store.hypotheses("t")[0].outcome == HypothesisOutcome.confirmed

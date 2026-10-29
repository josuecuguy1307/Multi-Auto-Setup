import asyncio
from pathlib import Path

import pytest

from envrec.adapters import RING0_KEYS, BuiltinRing0Adapter, ToolContext
from envrec.contracts import Evidence, Fact, FactStatus, Target, TargetKind
from envrec.rings import close_ring
from envrec.scoring import GroundTruth, GTField, load_ground_truth, score_target, values_match
from envrec.store import Store

ROOT = Path(__file__).resolve().parent.parent
TRUTH = ROOT / "ground_truth" / "this-machine.yaml"


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "s") as s:
        yield s


@pytest.fixture
def target(store):
    return store.put_target(Target(kind=TargetKind.host, ring=0, handle="h"))


def ev(provider="p"):
    return Evidence(provider=provider, probe="x", confidence=0.9)


def put(store, target, key, value, status=FactStatus.tested, providers=("p",)):
    from envrec.contracts import RawRef
    evidence = [ev(p) for p in providers]
    if status == FactStatus.verified:  # verified needs independent observations: a distinct raw capture per provider
        evidence = [e.model_copy(update={"raw_ref": RawRef(path=f"raw/{i}", sha256=str(i + 1) * 64)}) for i, e in enumerate(evidence)]
    store.write_fact(Fact(target_id=target.id, key=key, value=value, status=status, evidence=evidence))


def gt(**fields):
    return GroundTruth(machine="m", confirmed=True,
                       facts={k: GTField(value=v, confirmed=True) for k, v in fields.items()})


def test_every_outcome_is_distinguished(store, target):
    put(store, target, "os.name", "macOS")
    put(store, target, "hw.cpu_cores_logical", 10)
    store.write_fact(Fact(target_id=target.id, key="hw.cpu_model"))  # tried, unknown
    put(store, target, "serial.protocol", "nmea", FactStatus.hypothesis, ("sig",))
    put(store, target, "serial.protocol", "scpi", FactStatus.tested, ("replay",))  # contradiction
    report = score_target(store, target.id, gt(**{
        "os.name": "macOS", "hw.cpu_cores_logical": 8, "hw.cpu_model": "Apple M3",
        "serial.protocol": "nmea", "hw.ram_total_gib": 16.0}))
    assert {r.key: r.outcome for r in report.rows} == {
        "os.name": "correct", "hw.cpu_cores_logical": "wrong", "hw.cpu_model": "unknown",
        "serial.protocol": "contradicted", "hw.ram_total_gib": "missing"}


def test_false_verified_rate_counts_wrong_values_among_verified(store, target):
    put(store, target, "os.name", "macOS", FactStatus.verified, ("platform", "native"))
    put(store, target, "os.arch", "x86_64", FactStatus.verified, ("platform", "native"))  # claimed, wrong
    put(store, target, "os.kernel", "1", FactStatus.tested)
    report = score_target(store, target.id, gt(**{"os.name": "macOS", "os.arch": "arm64", "os.kernel": "9"}))
    assert (report.verified_total, report.verified_wrong) == (2, 1)
    assert report.false_verified_rate == 0.5


def test_false_verified_rate_is_none_when_nothing_is_verified(store, target):
    put(store, target, "os.name", "macOS")
    assert score_target(store, target.id, gt(**{"os.name": "macOS"})).false_verified_rate is None


def test_report_is_provisional_until_every_field_is_confirmed(store, target):
    put(store, target, "os.name", "macOS")
    unconfirmed = GroundTruth(machine="m", confirmed=True, facts={"os.name": GTField(value="macOS")})
    assert score_target(store, target.id, unconfirmed).provisional is True
    assert "PROVISIONAL" in score_target(store, target.id, unconfirmed).render()
    assert score_target(store, target.id, gt(**{"os.name": "macOS"})).provisional is False
    half = GroundTruth(machine="m", confirmed=False, facts={"os.name": GTField(value="macOS", confirmed=True)})
    assert score_target(store, target.id, half).provisional is True  # file-level flag counts too


def test_unscored_lists_known_facts_the_truth_does_not_mention(store, target):
    put(store, target, "os.name", "macOS")
    put(store, target, "os.arch", "arm64")
    store.write_fact(Fact(target_id=target.id, key="os.kernel"))  # unknown is not listed
    assert score_target(store, target.id, gt(**{"os.name": "macOS"})).unscored == ["os.arch"]


@pytest.mark.parametrize(("expected", "actual", "ok"), [
    ("macOS", " MACOS ", True), (True, 1, False), (8, 8, True), (8, 9, False),
    (16.0, 16.5, True), (16.0, 18.0, False), (["a", "b"], ["b", "a"], True), (["a"], ["a", "b"], False),
    ({"git": True, "ollama": False}, {"git": "git 2.1", "ollama": None, "node": "v22"}, True),
    ({"git": True}, {"git": None}, False),
    ({"git": "2.1"}, {"git": "2.1"}, True),
    (None, 1, False), (1, None, False),
])
def test_values_match(expected, actual, ok):
    assert values_match(expected, actual) is ok


def test_per_field_tolerance_overrides_the_default(store, target):
    put(store, target, "hw.disk_free_gib", 45.0)
    tight = GroundTruth(machine="m", facts={"hw.disk_free_gib": GTField(value=53.0)})
    loose = GroundTruth(machine="m", facts={"hw.disk_free_gib": GTField(value=53.0, rel_tolerance=0.25)})
    assert score_target(store, target.id, tight).rows[0].outcome == "wrong"
    assert score_target(store, target.id, loose).rows[0].outcome == "correct"


def test_ground_truth_rejects_keys_outside_the_namespace():
    with pytest.raises(Exception):
        GroundTruth(machine="m", facts={"bogus.key": GTField(value=1)})


def test_the_draft_ground_truth_loads_and_is_entirely_unconfirmed():
    truth = load_ground_truth(TRUTH)
    assert truth.confirmed is False and not any(f.confirmed for f in truth.facts.values())
    assert set(truth.facts) <= set(RING0_KEYS)  # nothing is scored that Ring 0 cannot produce


def test_ring0_end_to_end_scores_without_crashing_and_stays_provisional(store, target):
    adapter = BuiltinRing0Adapter()
    for spec in adapter.tool_specs():
        result = asyncio.run(adapter.run(spec.name, {}, ToolContext(store, target, "evt_x")))
        assert result.ok
        for fact in result.facts:
            store.write_fact(fact)
    close_ring(store, target, 0, RING0_KEYS, further_rings=False)
    report = score_target(store, target.id, load_ground_truth(TRUTH))
    assert len(report.rows) == len(load_ground_truth(TRUTH).facts)
    assert report.provisional
    assert report.count("missing") == 0  # every scored key was at least tried
    print("\n" + report.render())

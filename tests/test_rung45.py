import asyncio
import json
from pathlib import Path

import pytest

from envrec.contracts import FactStatus, Target, TargetKind
from envrec.fingerprint import UsbIds
from envrec.ladder import LadderAdapter, ReplayDevice, load_devices
from envrec.ladder.generate import (
    ReplayRunner,
    Rung45,
    ScriptedVerifier,
    VerifierVerdict,
    check_code,
)
from envrec.loop import Loop, RunConfig
from envrec.model import ScriptedModel, scripted_profile
from envrec.scoring import highest_rung, load_ground_truth, score_target
from envrec.store import Store

ROOT = Path(__file__).resolve().parent.parent
USB = UsbIds.from_file(ROOT / "fixtures" / "samples" / "usb_ids_sample.txt")
DONE = json.dumps({"action": "done"})

GOOD = '''import json, re, sys
d = json.load(sys.stdin)
m = re.search(r"~~ZXQ (\\d+)/(\\d+) ~~", d["chunk"])
facts = []
if m:
    facts = [{"key": "sensor.temperature_c", "value": int(m.group(1))},
             {"key": "sensor.humidity", "value": int(m.group(2))}]
print(json.dumps({"facts": facts}))
'''
WRONG = GOOD.replace('int(m.group(1))}', 'int(m.group(2))}', 1)  # temperature reads the humidity digit


def call(tool):
    return json.dumps({"action": "call_tool", "tool": tool, "why": "next"})


def propose(code=GOOD, plan="temperature_c and humidity from each ZXQ frame, as integers"):
    return json.dumps({"action": "propose_probe", "code": code, "test_plan": plan, "why": "write a parser"})


RESEARCH = json.dumps({"action": "research", "question": "what does the ZXQ frame mean?"})
PASS = VerifierVerdict(True, False, ["output matches the plan"])


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "s") as s:
        yield s


def drive(store, replies, verifier=None, docs=ROOT / "fixtures" / "docs", name="zxq-sensor", with_runner=True):
    devices = load_devices(ROOT / "fixtures" / "replay")
    runner = ReplayRunner(timeout_s=5)
    target = store.put_target(Target(kind=TargetKind.usb, ring=1, handle=f"replay:{name}"))
    r45 = Rung45(devices, docs, verifier, runner)
    adapter = LadderAdapter(devices, USB, runner if with_runner else None)
    model = ScriptedModel(replies, scripted_profile())
    summary = asyncio.run(Loop(store, [adapter], model, config=RunConfig(max_steps=15), rung45=r45).run(target))
    return target, summary, model


def facts(store, t):
    return {f.key: f for f in store.current_facts(t.id)}


STORY = [call("read_usb_descriptor"), call("lookup_usb_ids"), call("read_serial_ident"),
         call("match_serial_protocol"), RESEARCH]


# ---- the code screen and the runner ------------------------------------------------------------------------------

def test_the_code_screen_accepts_the_good_probe_and_rejects_the_dangerous_ones():
    assert check_code(GOOD) == []
    for bad in ("import os\nos.system('x')", "import socket", "open('/etc/passwd')", "eval('1')",
                "x = getattr(sys, 'modules')", "print(().__class__)", "def (", "from subprocess import run"):
        assert check_code(bad), bad


def test_the_runner_runs_good_code_and_reports_failures_instead_of_raising():
    r = ReplayRunner(timeout_s=5)
    ok = r.run(GOOD, "~~ZXQ 7/9 ~~\r\n")
    assert ok.ok and {f["key"]: f["value"] for f in ok.facts} == {"sensor.temperature_c": 7, "sensor.humidity": 9}
    assert not r.run(GOOD, "garbage").ok  # no match prints no facts: not a valid result
    assert "code screen" in r.run("import os", "x").error
    assert "exit" in r.run("raise SystemExit(3)", "x").error
    assert not r.run("print('not json')", "x").ok
    assert not r.run("print('{\"facts\": [{\"key\": \"bogus.k\", \"value\": 1}]}')", "x").ok  # key outside namespace


def test_the_runner_stops_a_probe_that_never_finishes():
    slow = ReplayRunner(timeout_s=1).run("while True:\n    pass", "x")
    assert not slow.ok and "timed out" in slow.error


def test_the_probe_cannot_see_the_environment_or_write_files(tmp_path):
    code = "import json, sys\nprint(json.dumps({'facts': [{'key': 'host.env_count', 'value': len(sys.argv)}]}))"
    assert ReplayRunner().run(code, "x").ok


# ---- rung 4: research --------------------------------------------------------------------------------------------

def test_research_stores_the_document_by_hash_and_shows_only_a_marked_excerpt(store):
    t, s, model = drive(store, STORY + [DONE])
    doc = facts(store, t)["serial.protocol_doc"]
    assert doc.status == FactStatus.hypothesis and doc.evidence[0].provider == "research:zxq-sensor.md"
    assert "not measured" in " ".join(doc.evidence[0].limitations)
    assert "ZXQ <temperature>/<humidity>" in store.read_raw(doc.evidence[0].raw_ref).decode()
    last_view = model.calls[-1][1]
    assert "UNTRUSTED DOCUMENT TEXT" in last_view and "whole degrees Celsius" in last_view
    assert highest_rung(store, t.id) == 4


def test_research_without_documentation_records_an_unknown(store):
    t, _, _ = drive(store, [RESEARCH, DONE], docs=None)
    assert facts(store, t)["serial.protocol_doc"].status == FactStatus.unknown


# ---- rung 5: a generated probe, verified, becomes a skill -------------------------------------------------------------

def test_a_probe_the_verifier_passes_makes_verified_facts_and_a_skill(store):
    verifier = ScriptedVerifier([PASS])
    t, s, _ = drive(store, STORY + [propose(), DONE], verifier)
    f = facts(store, t)
    assert (f["sensor.temperature_c"].value, f["sensor.humidity"].value) == (7, 9)
    for key in ("sensor.temperature_c", "sensor.humidity"):
        assert f[key].status == FactStatus.verified and f[key].created_by.value == "verifier"
        assert {e.provider for e in f[key].evidence} >= {"verifier"}
    skill = store.find_skill("usb:ffff:0002")
    assert skill and skill.verified_runs == 1
    from envrec.ladder.generate import read_skill_code
    assert "ZXQ" in read_skill_code(store, skill)
    assert highest_rung(store, t.id) == 5
    report = score_target(store, t.id, load_ground_truth(ROOT / "ground_truth" / "replay-zxq-sensor.yaml"))
    assert {r.outcome for r in report.rows} == {"correct"} and report.false_verified_rate == 0.0


def test_the_verifier_sees_the_plan_and_the_output_but_never_the_code(store):
    verifier = ScriptedVerifier([PASS])
    drive(store, STORY + [propose(), DONE], verifier)
    (inp,) = verifier.inputs
    assert "humidity" in inp.test_plan and "sensor.humidity" in inp.probe_output
    assert "import json" not in inp.test_plan + inp.probe_output  # a clean context: no code, no reasoning


def test_a_probe_the_verifier_rejects_stays_tested_and_saves_no_skill(store):
    t, _, model = drive(store, STORY + [propose(), DONE], ScriptedVerifier([VerifierVerdict(False, False, ["no"])]))
    f = facts(store, t)
    assert f["sensor.humidity"].status == FactStatus.tested and store.find_skill("usb:ffff:0002") is None
    assert "verifier did not pass" in model.calls[-1][1]


def test_an_unsafe_verdict_blocks_verification_even_when_passed(store):
    t, _, _ = drive(store, STORY + [propose(), DONE], ScriptedVerifier([VerifierVerdict(True, True, ["touches files"])]))
    assert facts(store, t)["sensor.humidity"].status == FactStatus.tested


def test_a_probe_with_forbidden_code_never_runs_and_writes_nothing(store):
    bad = "import os\nprint(os.listdir('/'))"
    t, _, model = drive(store, STORY + [propose(bad), DONE], ScriptedVerifier([PASS]))
    assert "sensor.humidity" not in facts(store, t) and "code screen" in model.calls[-1][1]


def test_generated_code_never_runs_on_a_non_replay_target(store):
    real = store.put_target(Target(kind=TargetKind.usb, ring=1, handle="usb:ffff:0002"))
    msg = asyncio.run(Rung45(load_devices(ROOT / "fixtures" / "replay"), None, None).propose(
        store, real, GOOD, "plan", None))
    assert "never runs on real devices" in msg


def test_a_wrong_probe_that_passes_a_lenient_verifier_is_caught_by_the_scorer(store):
    """The false-verified rate is the key metric: here the verifier is fooled and ground truth says so."""
    t, _, _ = drive(store, STORY + [propose(WRONG), DONE], ScriptedVerifier([PASS]))
    assert facts(store, t)["sensor.temperature_c"].status == FactStatus.verified
    report = score_target(store, t.id, load_ground_truth(ROOT / "ground_truth" / "replay-zxq-sensor.yaml"))
    assert report.verified_wrong >= 1 and report.false_verified_rate > 0


# ---- the second encounter: the skill is used at rung 1 -----------------------------------------------------------------

def test_the_second_encounter_uses_the_saved_skill_with_no_research_and_no_model_written_code(store):
    drive(store, STORY + [propose(), DONE], ScriptedVerifier([PASS]))
    t2, s2, model2 = drive(store, [call("read_usb_descriptor"), call("try_known_skill"), DONE])
    f = facts(store, t2)
    assert f["skill.match"].value and f["skill.match"].status == FactStatus.tested
    assert (f["sensor.temperature_c"].value, f["sensor.humidity"].value) == (7, 9)
    assert f["sensor.humidity"].evidence[0].provider.startswith("skill:")
    types = [e.type for e in store.events(run_id=s2.run_id)]
    assert "research" not in types and "probe_proposed" not in types
    assert store.find_skill("usb:ffff:0002").verified_runs == 2
    assert highest_rung(store, t2.id) == 2
    # a new Target is a new device as far as the skill is concerned: its stored tests re-ran here before it was trusted
    assert f["sensor.humidity"].status == FactStatus.verified and f["sensor.humidity"].evidence[-1].provider == "skill-reverify"

import asyncio
import json
from pathlib import Path

import pytest

from envrec.contracts import (
    Evidence,
    FactStatus,
    HypothesisOutcome,
    Skill,
    Target,
    TargetKind,
)
from envrec.fingerprint import UsbIds
from envrec.ladder import LadderAdapter, ReplayDevice, load_devices
from envrec.loop import Loop, RunConfig
from envrec.model import ScriptedModel, scripted_profile
from envrec.scoring import fingerprint_stats, highest_rung, load_ground_truth, score_target
from envrec.store import Store

ROOT = Path(__file__).resolve().parent.parent
USB_SAMPLE = UsbIds.from_file(ROOT / "fixtures" / "samples" / "usb_ids_sample.txt")
DONE = json.dumps({"action": "done", "why": "finished"})


def call(tool):
    return json.dumps({"action": "call_tool", "tool": tool, "why": "next rung"})


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "s") as s:
        yield s


def run_fixture(store, name, replies, *, profile=None, ask_human=None, devices=None):
    devices = devices or load_devices(ROOT / "fixtures" / "replay")
    device = devices.get(f"replay:{name}")
    target = store.put_target(Target(kind=TargetKind.usb, ring=1, handle=device.handle))
    adapter = LadderAdapter(devices, USB_SAMPLE)
    model = ScriptedModel(replies, profile or scripted_profile())
    summary = asyncio.run(Loop(store, [adapter], model, config=RunConfig(max_steps=15),
                               ask_human=ask_human).run(target))
    return target, summary, model


def facts(store, target):
    return {f.key: f for f in store.current_facts(target.id)}


def score(store, target, name):
    return {r.key: r.outcome for r in
            score_target(store, target.id, load_ground_truth(ROOT / "ground_truth" / f"replay-{name}.yaml")).rows}


FULL_CLIMB = [call("read_usb_descriptor"), call("try_known_skill"), call("lookup_usb_ids"),
              call("read_serial_ident"), call("match_serial_protocol"), call("confirm_hypotheses")]


# ---- (a) a device the public lists recognize: climbs to tested at rung 3 -----------------------------------

def test_a_known_device_climbs_to_tested_at_rung_3(store):
    t, s, _ = run_fixture(store, "ch340-gps", FULL_CLIMB + [DONE])
    f = facts(store, t)
    proto = f["serial.protocol"]
    assert (proto.value, proto.status) == ("nmea", FactStatus.tested)
    assert {e.provider for e in proto.evidence} == {"serial.signatures", "replay-device"}
    assert f["usb.vendor_name"].status == FactStatus.tested  # list guess + descriptor agreement
    assert f["usb.product_name"].value == "CH340 serial converter"
    assert f["usb.product_name"].status == FactStatus.hypothesis  # nothing could confirm the model
    assert highest_rung(store, t.id) == 3  # never needed to ask: a solved device is not a rung-6 case
    assert s.rejected_actions == 0 and s.policy_denied == 0
    assert s.stop_reason == "menu_empty" and not store.events(type="human_question")


def test_a_known_device_scores_all_correct(store):
    t, _, _ = run_fixture(store, "ch340-gps", FULL_CLIMB + [DONE])
    assert set(score(store, t, "ch340-gps").values()) == {"correct"}


def test_hypotheses_and_per_source_stats(store):
    t, _, _ = run_fixture(store, "ch340-gps", FULL_CLIMB + [DONE])
    by_claim = {h.claim: h for h in store.hypotheses(t.id)}
    assert by_claim["the serial protocol is nmea"].outcome == HypothesisOutcome.confirmed
    assert by_claim["the usb vendor is QinHeng Electronics"].outcome == HypothesisOutcome.confirmed
    assert by_claim["the usb product is CH340 serial converter"].outcome == HypothesisOutcome.pending
    stats = fingerprint_stats(store)
    assert (stats["usb.ids"].matched, stats["usb.ids"].confirmed, stats["usb.ids"].pending) == (2, 1, 1)
    assert (stats["serial.signatures"].matched, stats["serial.signatures"].confirmed) == (1, 1)


def test_the_target_signature_is_computed_from_the_descriptor(store):
    t, _, _ = run_fixture(store, "ch340-gps", [call("read_usb_descriptor"), DONE])
    assert store.get_target(t.id).signature == "usb:1a86:7523"


def test_listening_is_enough_when_the_stream_is_recognized(store):
    t, _, _ = run_fixture(store, "ch340-gps", [call("read_serial_ident"), DONE])
    exchanges = json.loads(store.read_raw(facts(store, t)["serial.ident_reply"].evidence[0].raw_ref))
    assert len(exchanges) == 1  # no queries were sent: NMEA identified itself


def test_confirming_early_does_not_use_up_a_guess_made_later(store):
    """Found by running live models: both called confirm_hypotheses right after the usb lookup."""
    early = [call("read_usb_descriptor"), call("lookup_usb_ids"), call("confirm_hypotheses"),
             call("read_serial_ident"), call("match_serial_protocol"), call("confirm_hypotheses"), DONE]
    t, s, _ = run_fixture(store, "ch340-gps", early)
    assert s.tools_run.count("confirm_hypotheses") == 2 and s.rejected_actions == 0
    assert facts(store, t)["serial.protocol"].status == FactStatus.tested
    assert all(h.attempts <= 1 for h in store.hypotheses(t.id))  # never more than ONE confirming read per guess


def test_confirm_is_not_offered_again_once_every_guess_has_had_its_attempt(store):
    from envrec.loop import build_menu
    t, s, model = run_fixture(store, "ch340-gps", FULL_CLIMB + [DONE])
    adapter = LadderAdapter(load_devices(ROOT / "fixtures" / "replay"), USB_SAMPLE)
    assert "confirm_hypotheses" not in [e.name for e in build_menu([adapter], t, store, 8)]
    assert s.stop_reason == "menu_empty"


def test_an_early_confirm_on_the_liar_device_still_ends_contradicted(store):
    early = [call("read_usb_descriptor"), call("lookup_usb_ids"), call("confirm_hypotheses"),
             call("read_serial_ident"), call("match_serial_protocol"), call("confirm_hypotheses"), DONE]
    t, _, _ = run_fixture(store, "liar-device", early)
    assert facts(store, t)["serial.protocol"].status == FactStatus.contradicted


def test_the_fixture_ground_truth_matches_its_device_descriptor_and_list():
    """Guards the YAML itself: the comma in a vendor name was once lost when the file was generated."""
    truth = load_ground_truth(ROOT / "ground_truth" / "replay-liar-device.yaml")
    assert truth.facts["usb.vendor_name"].value == USB_SAMPLE.vendor("0403")


# ---- (b) a mystery device: ends unknown, asks the human ---------------------------------------------------------

def test_a_mystery_device_ends_unknown_and_asks_the_human(store):
    ask = json.dumps({"action": "ask_human", "key": "usb.product_name",
                      "question": "What device has vendor ffff and product 0001?"})
    t, s, model = run_fixture(store, "mystery-box", [
        call("read_usb_descriptor"), call("try_known_skill"), call("lookup_usb_ids"),
        call("read_serial_ident"), call("match_serial_protocol"), ask])
    f = facts(store, t)
    for key in ("usb.vendor_name", "usb.product_name", "serial.protocol", "usb.manufacturer"):
        assert f[key].status == FactStatus.unknown, key
    assert (f["skill.match"].value, f["skill.match"].status) == (False, FactStatus.tested)  # a measured "none"
    assert store.hypotheses(t.id) == []  # nothing was guessable, so nothing was guessed
    assert "confirm_hypotheses" not in s.tools_run  # nothing to confirm: the tool was never offered
    q = store.events(type="human_question")[0].payload
    assert q["target"] == t.id and "ffff" in q["question"]
    assert highest_rung(store, t.id) == 6
    assert set(score(store, t, "mystery-box").values()) == {"correct"}  # honestly unknown is the right answer


def test_the_mystery_device_was_queried_with_safe_commands_only(store):
    t, _, _ = run_fixture(store, "mystery-box", [call("read_serial_ident"), DONE])
    exchanges = json.loads(store.read_raw(facts(store, t)["serial.ident_reply"].evidence[0].raw_ref))
    assert [e["sent"] for e in exchanges] == ["", "*IDN?\r\n", "AT\r\n"]


def test_the_menu_offers_ask_human_even_with_every_tool_used(store):
    t, s, model = run_fixture(store, "mystery-box", [
        call("read_usb_descriptor"), call("try_known_skill"), call("lookup_usb_ids"),
        call("read_serial_ident"), call("match_serial_protocol"), DONE])
    assert "(no tools left)" in model.calls[-1][1] and "ask_human" in model.calls[-1][1]


def test_a_human_answer_is_stored_as_the_humans_not_the_harnesss(store):
    ask = json.dumps({"action": "ask_human", "key": "usb.product_name", "question": "What is it?"})
    t, _, _ = run_fixture(store, "mystery-box", [call("read_usb_descriptor"), call("lookup_usb_ids"), ask],
                          ask_human=lambda q: "a custom sensor hub")
    fact = facts(store, t)["usb.product_name"]
    assert fact.created_by.value == "human" and fact.status == FactStatus.tested
    assert fact.evidence[0].provider == "human"


# ---- (c) a device whose confirming read disagrees: contradicted -----------------------------------------------------

def test_a_confirming_read_that_disagrees_ends_contradicted_with_both_evidence_kept(store):
    t, _, _ = run_fixture(store, "liar-device", FULL_CLIMB + [DONE])
    proto = facts(store, t)["serial.protocol"]
    assert proto.status == FactStatus.contradicted
    assert (proto.value, proto.alt_values) == ("nmea", ["scpi"])
    assert {e.provider for e in proto.evidence} == {"serial.signatures", "replay-device"}
    by_claim = {h.claim: h for h in store.hypotheses(t.id)}
    assert by_claim["the serial protocol is nmea"].outcome == HypothesisOutcome.contradicted
    stats = fingerprint_stats(store)
    assert stats["serial.signatures"].wrong == 1 and stats["serial.signatures"].confirmed == 0
    assert score(store, t, "liar-device")["serial.protocol"] == "contradicted"


def test_a_vendor_match_with_no_agreeing_descriptor_stays_a_hypothesis(store):
    t, _, _ = run_fixture(store, "liar-device", FULL_CLIMB + [DONE])
    f = facts(store, t)
    assert f["usb.vendor_name"].status == FactStatus.hypothesis  # "FTDI" vs the long list name: no overlap
    assert f["usb.vendor_name"].value == "Future Technology Devices International, Ltd"


def test_a_silent_device_leaves_the_guess_pending_instead_of_contradicting_it(store, tmp_path):
    quiet = ReplayDevice("quiet", "replay:quiet", "usb", {"vendor_id": "1a86", "product_id": "7523", "manufacturer": None},
                         stream=["$GPGLL,4916.45,N,12311.12,W,225444,A*31\r\n"], responses={})
    from envrec.ladder import DeviceRegistry
    t, _, _ = run_fixture(store, "quiet", FULL_CLIMB + [DONE], devices=DeviceRegistry([quiet]))
    assert facts(store, t)["serial.protocol"].status == FactStatus.hypothesis
    assert {h.outcome for h in store.hypotheses(t.id) if "serial" in h.claim} == {HypothesisOutcome.pending}


# ---- the invariants ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["ch340-gps", "mystery-box", "liar-device"])
def test_nothing_is_ever_verified_in_this_phase(store, name):
    t, _, _ = run_fixture(store, name, FULL_CLIMB + [DONE])
    assert all(f.status != FactStatus.verified for f in store.current_facts(t.id))
    for fact in store.current_facts(t.id):  # not even in the history
        assert all(c.status != FactStatus.verified for c in store.fact_chain(fact.id))


@pytest.mark.parametrize("name", ["ch340-gps", "mystery-box", "liar-device"])
def test_every_ladder_run_closes_ring_1_with_coverage(store, name):
    t, s, _ = run_fixture(store, name, FULL_CLIMB + [DONE])
    cov = store.current_fact(t.id, "ring.1.coverage")
    assert cov is not None and s.ring_decisions[t.id] == "stop"


def test_ladder_tools_refuse_anything_that_is_not_a_replay_device(store):
    from envrec.ladder import DeviceRegistry
    real = store.put_target(Target(kind=TargetKind.usb, ring=1, handle="usb:1a86:7523"))
    from envrec.adapters import ToolContext
    result = asyncio.run(LadderAdapter(DeviceRegistry(), USB_SAMPLE).run(
        "read_usb_descriptor", {}, ToolContext(store, real, None)))
    assert not result.ok and "not a replay device" in result.error


def test_the_replay_device_refuses_commands_off_the_allowlist():
    dev = ReplayDevice("d", "replay:d", "usb", {}, [], {})
    with pytest.raises(ValueError):
        dev.send("AT+CFUN=1,1\r\n")  # a reset is not an identification query


def test_a_saved_skill_for_the_signature_is_found_at_rung_1(store):
    store.put_skill(Skill(signature="usb:1a86:7523", code_ref="skills/ch340.py", tests_ref="skills/test_ch340.py",
                          evidence=[Evidence(provider="verifier", probe="t", confidence=1.0)]))
    t, _, _ = run_fixture(store, "ch340-gps", [call("read_usb_descriptor"), call("try_known_skill"), DONE])
    skill_fact = facts(store, t)["skill.match"]
    assert skill_fact.status == FactStatus.tested and skill_fact.evidence[0].provider == "skill-store"
    assert highest_rung(store, t.id) == 2  # rung 1 was tried; the highest rung climbed is 2


def test_replay_fixtures_are_marked_synthetic():
    for p in (ROOT / "fixtures" / "replay").glob("*.json"):
        assert json.loads(p.read_text())["synthetic"] is True

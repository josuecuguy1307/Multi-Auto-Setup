import pytest
from pydantic import ValidationError

from envrec.contracts import (
    Action,
    ActionType,
    Actor,
    Coverage,
    CreatedBy,
    Decision,
    Event,
    Evidence,
    Fact,
    FactStatus,
    Hypothesis,
    ModelProfile,
    PolicyDecision,
    ProbeResult,
    RawRef,
    RingCoverage,
    RingDecision,
    Skill,
    Target,
    TargetKind,
    Tier,
    ToolSpec,
    Unknown,
    readable_error,
    reconcile,
)

SHA = "a" * 64


def ev(provider="builtin", probe="p", conf=0.9, **kw):
    return Evidence(provider=provider, probe=probe, confidence=conf, **kw)


def fact(key="host.kind", value="laptop", status=FactStatus.tested, evidence=None, **kw):
    if evidence is None and status != FactStatus.unknown and status != FactStatus.hypothesis:
        evidence = [ev()]
    return Fact(target_id="tgt_1", key=key, value=value, status=status, evidence=evidence or [], **kw)


SAMPLES = [
    RawRef(path="raw/ab/abc", sha256=SHA),
    ev(raw_ref=RawRef(path="raw/x", sha256=SHA), limitations=["read only"], note="usb.ids line"),
    Target(kind=TargetKind.usb, ring=1, handle="usb:1a86:7523", authorized=True, parent="tgt_0"),
    fact(),
    fact(status=FactStatus.unknown, value=None),
    ToolSpec(name="read_cpu", ring=0, tier=Tier.observe, target_kinds=[TargetKind.host],
             one_line="read cpu model and core count", source="builtin", tags=["safe"]),
    ProbeResult(ok=True, facts=[fact()], raw_ref=RawRef(path="raw/x", sha256=SHA),
                coverage=Coverage(status="partial", checked=["cpu"], unknown=["gpu"]),
                unknowns=[Unknown(key="hw.gpu", question="which gpu")]),
    Action(action=ActionType.call_tool, tool="read_cpu", why="need cpu facts"),
    PolicyDecision(decision=Decision.allow, reason="observe tier", tool="read_cpu"),
    Event(actor=Actor.loop, type="run_start", payload={"n": 1, "ok": True, "tags": ["a"]}),
    Hypothesis(target_id="tgt_1", claim="vendor is QinHeng", rung=3, based_on=["fact_1"]),
    Skill(signature="usb:1a86:*", code_ref="skills/a.py", tests_ref="skills/test_a.py", evidence=[ev()]),
    RingCoverage(ring=0, checked=["os"], unknown=["gpu"], decision=RingDecision.extend),
    ModelProfile(name="small", model_id="some-model", endpoint="http://localhost:11434/v1",
                 context_tokens=8192, view_budget_facts=8, menu_max=6),
]


@pytest.mark.parametrize("obj", SAMPLES, ids=lambda o: type(o).__name__)
def test_json_round_trip(obj):
    again = type(obj).model_validate_json(obj.model_dump_json())
    assert again == obj


@pytest.mark.parametrize("obj", SAMPLES, ids=lambda o: type(o).__name__)
def test_unknown_fields_are_forbidden(obj):
    data = obj.model_dump(mode="json")
    data["surprise"] = 1
    with pytest.raises(ValidationError):
        type(obj).model_validate(data)


# ---- Action -------------------------------------------------------------------------------

def test_invalid_action_gives_a_readable_error():
    with pytest.raises(ValidationError) as exc:
        Action.model_validate_json('{"action": "call_tool"}')
    text = readable_error(exc.value)
    assert "requires: tool" in text
    assert "Traceback" not in text


@pytest.mark.parametrize(
    ("payload", "needle"),
    [
        ({"action": "dance"}, "action"),
        ({"action": "extend_to"}, "ring"),
        ({"action": "extend_to", "ring": 9}, "ring"),
        ({"action": "ask_human"}, "question"),
        ({"action": "research"}, "question"),
        ({"action": "propose_skill"}, "skill_draft"),
        ({"action": "expand_fact"}, "fact_id or key"),
        ({"action": "record_unknown", "key": "bogus.thing"}, "unknown prefix"),
    ],
)
def test_action_rejections(payload, needle):
    with pytest.raises(ValidationError) as exc:
        Action.model_validate(payload)
    assert needle in readable_error(exc.value)


def test_why_over_the_cap_is_rejected():
    ok = Action(action=ActionType.done, why=" ".join(["w"] * 30))
    assert ok.action == ActionType.done
    with pytest.raises(ValidationError) as exc:
        Action(action=ActionType.done, why=" ".join(["w"] * 31))
    assert "at most 30 words" in readable_error(exc.value)


def test_valid_actions_of_every_type():
    Action(action=ActionType.call_tool, tool="t")
    Action(action=ActionType.extend_to, ring=1)
    Action(action=ActionType.expand_fact, key="host.kind")
    Action(action=ActionType.research, question="what is this chip")
    Action(action=ActionType.propose_skill, skill_draft={"name": "x"})
    Action(action=ActionType.record_unknown)
    Action(action=ActionType.ask_human, question="what is this device?")
    Action(action=ActionType.done)


# ---- Fact rules ----------------------------------------------------------------------------

def test_fact_without_evidence_cannot_pass_hypothesis():
    Fact(target_id="t", key="usb.vendor_name", value="X", status=FactStatus.hypothesis)
    for status in (FactStatus.tested, FactStatus.verified, FactStatus.contradicted):
        with pytest.raises(ValidationError):
            Fact(target_id="t", key="usb.vendor_name", value="X", status=status)


def raw(n: str):
    from envrec.contracts import RawRef
    return RawRef(path=f"raw/{n}", sha256=n * 64)


def test_verified_needs_the_verifier_or_two_independent_observations():
    """Independent means distinct raw captures AND distinct channels; two provider names over one capture, or over no capture, are not."""
    def verified(evidence, **kw):
        return Fact(target_id="t", key="os.name", value="macOS", status=FactStatus.verified, evidence=evidence, **kw)
    for not_enough in ([ev("builtin"), ev("builtin", probe="other")],                                         # one provider, no capture
                       [ev("builtin"), ev("sw_vers")],                                                        # two provider names, no capture at all
                       [ev("builtin", raw_ref=raw("a")), ev("sw_vers", raw_ref=raw("a"))],                    # two provider names over ONE capture
                       [ev("builtin", raw_ref=raw("a"), channel="ssh"), ev("sw_vers", raw_ref=raw("b"), channel="ssh")]):  # two captures over ONE channel
        with pytest.raises(ValidationError) as exc:
            verified(not_enough)
        assert "distinct raw captures AND distinct channels" in readable_error(exc.value)
    ok = verified([ev("builtin", raw_ref=raw("a"), channel="usb"), ev("builtin", raw_ref=raw("b"), channel="serial")])
    assert ok.status == FactStatus.verified and len(ok.evidence) == 2
    assert verified([ev("verifier")], created_by=CreatedBy.verifier).created_by == CreatedBy.verifier


def test_contradicted_keeps_both_evidence_entries():
    with pytest.raises(ValidationError):
        Fact(target_id="t", key="os.name", value="a", status=FactStatus.contradicted, evidence=[ev()])


def test_unknown_fact_has_no_value():
    with pytest.raises(ValidationError):
        Fact(target_id="t", key="os.name", value="x", status=FactStatus.unknown)


@pytest.mark.parametrize("key", ["vendor", "bogus.thing", "usb.", "USB.vendor", "usb.vendor name", ""])
def test_bad_keys_are_rejected(key):
    with pytest.raises(ValidationError):
        Fact(target_id="t", key=key)


@pytest.mark.parametrize("prefix", sorted({"host", "os", "hw", "usb", "serial", "ble", "net", "instr",
                                           "signal", "app", "fw", "repo", "sensor", "ring", "skill"}))
def test_every_namespace_prefix_is_accepted(prefix):
    Fact(target_id="t", key=f"{prefix}.x")


# ---- supersede and contradiction --------------------------------------------------------

def test_same_value_merges_evidence_and_supersedes():
    hyp = fact("usb.vendor_name", "QinHeng", FactStatus.hypothesis, [ev("usb.ids", "lookup")])
    tested = fact("usb.vendor_name", "QinHeng", FactStatus.tested, [ev("builtin", "descriptor_read")])
    r = reconcile(hyp, tested)
    assert r.changed and r.fact.supersedes == hyp.id
    assert r.fact.status == FactStatus.tested  # hypothesis + one test is not verified
    assert {e.provider for e in r.fact.evidence} == {"usb.ids", "builtin"}


def test_two_independent_tested_sources_become_verified():
    a = fact("os.name", "macOS", FactStatus.tested, [ev("builtin", "platform", raw_ref=raw("a"))])
    b = fact("os.name", "macOS", FactStatus.tested, [ev("sw_vers", "cmd", raw_ref=raw("b"))])
    r = reconcile(a, b)
    assert r.fact.status == FactStatus.verified and len(r.fact.evidence) == 2


def test_two_provider_names_over_one_capture_stay_tested():
    a = fact("os.name", "macOS", FactStatus.tested, [ev("builtin", "platform", raw_ref=raw("a"))])
    b = fact("os.name", "macOS", FactStatus.tested, [ev("sw_vers", "cmd", raw_ref=raw("a"))])
    assert reconcile(a, b).fact.status == FactStatus.tested


def test_same_provider_twice_is_not_independent():
    a = fact("os.name", "macOS", FactStatus.tested, [ev("builtin", "platform")])
    b = fact("os.name", "macOS", FactStatus.tested, [ev("builtin", "uname")])
    assert reconcile(a, b).fact.status == FactStatus.tested


def test_disagreement_is_contradicted_and_keeps_both_evidence():
    hyp = fact("serial.protocol", "nmea", FactStatus.hypothesis, [ev("signatures", "match")])
    read = fact("serial.protocol", "scpi", FactStatus.tested, [ev("replay", "ident_read")])
    r = reconcile(hyp, read)
    assert r.fact.status == FactStatus.contradicted
    assert len(r.fact.evidence) == 2
    assert r.fact.value == "nmea" and r.fact.alt_values == ["scpi"]
    assert r.fact.supersedes == hyp.id and r.superseded == hyp


def test_supersede_chain_is_followable():
    f1 = fact("os.version", "14", FactStatus.hypothesis, [ev("a")])
    f2 = reconcile(f1, fact("os.version", "14", FactStatus.tested, [ev("b")])).fact
    f3 = reconcile(f2, fact("os.version", "15", FactStatus.tested, [ev("c")])).fact
    assert f2.supersedes == f1.id and f3.supersedes == f2.id
    assert f3.status == FactStatus.contradicted and len(f3.evidence) == 3


def test_same_source_rereading_a_changed_value_supersedes_instead_of_contradicting():
    old = fact("hw.disk_free_gib", 70.0, FactStatus.tested, [ev("stdlib", "read_disks")])
    new = fact("hw.disk_free_gib", 68.5, FactStatus.tested, [ev("stdlib", "read_disks")])
    r = reconcile(old, new)
    assert r.fact.status == FactStatus.tested and r.fact.value == 68.5
    assert r.fact.supersedes == old.id and len(r.fact.evidence) == 1


def test_a_different_provider_disagreeing_is_still_a_contradiction():
    old = fact("hw.disk_free_gib", 70.0, FactStatus.tested, [ev("stdlib", "read_disks")])
    other = fact("hw.disk_free_gib", 50.0, FactStatus.tested, [ev("native", "df")])
    assert reconcile(old, other).fact.status == FactStatus.contradicted


def test_unknown_never_overwrites_knowledge_but_knowledge_replaces_unknown():
    known = fact("hw.ram", 16, FactStatus.tested)
    unknown = fact("hw.ram", None, FactStatus.unknown)
    assert reconcile(known, unknown).changed is False
    r = reconcile(unknown, known)
    assert r.fact.value == 16 and r.fact.supersedes == unknown.id


def test_repeating_the_same_observation_changes_nothing():
    a = fact("hw.ram", 16, FactStatus.tested, [ev("builtin", "psutil")])
    again = fact("hw.ram", 16, FactStatus.tested, [ev("builtin", "psutil")])
    assert reconcile(a, again).changed is False


# ---- the other contracts --------------------------------------------------------------

def test_tool_one_line_is_capped_at_twelve_words():
    base = dict(name="t", ring=0, tier=Tier.observe, target_kinds=[TargetKind.host], source="builtin")
    ToolSpec(one_line=" ".join(["w"] * 12), **base)
    with pytest.raises(ValidationError):
        ToolSpec(one_line=" ".join(["w"] * 13), **base)
    with pytest.raises(ValidationError):
        ToolSpec(one_line="", **base)


def test_failed_probe_result_needs_an_error():
    with pytest.raises(ValidationError):
        ProbeResult(ok=False)
    assert ProbeResult(ok=False, error="timed out").error == "timed out"


def test_raw_ref_requires_a_real_sha256():
    with pytest.raises(ValidationError):
        RawRef(path="x", sha256="nothex")


def test_ring_coverage_is_stored_as_a_ring_fact():
    f = RingCoverage(ring=0, checked=["os"], unknown=["gpu"], decision=RingDecision.extend).to_fact("tgt_1")
    assert f.key == "ring.0.coverage"
    assert f.value["decision"] == "extend"
    assert f.status == FactStatus.tested


def test_model_profile_carries_no_secret_only_an_env_var_name():
    assert "api_key" not in ModelProfile.model_fields
    assert "api_key_env" in ModelProfile.model_fields

"""One catalog, with evidence (slice A): entry-level evidence_state, domains, license/pin/hash rules, researched leads merged by the same loader and held to the same guard, public evidence tiers
and the line the route explanation prints. SIMULATED-LOCAL: no process, no network."""
from __future__ import annotations

import copy
import json

import pytest

from envrec_coordinator.router import DictFacts, Router
from envrec_coordinator.router import evidence as EV
from envrec_coordinator.router.model import EntryChat, Task
from envrec_net import envrun as E
from tests.test_router import ENVS, admission, machine, policy, task

HEX = "a" * 64
LEAD = {"id": "finlead", "title": "A finance harness", "subtype": "own_harness", "domains": ["finance"], "task_types": ["numeric-analysis"], "source_url": "https://github.com/example/finlead",
        "license": {"spdx": "MIT", "note": "LICENSE read in the fetched page"}, "pin": "b" * 40, "retrieved": "2026-11-02", "evidence_state": "hypothesis",
        "fetched": {"url": "https://api.github.com/repos/example/finlead", "capture_sha256": HEX}, "accesses": [{"id": "finlead:local", "kind": "local", "cost_class": "local"}]}


def write_lead(tmp_path, *rows):
    (tmp_path / "environments.json").write_text(json.dumps({"environments": list(rows)}))
    return tmp_path


# ---- the guard ---------------------------------------------------------------------------------------------------------------------------------------------------------------

def test_the_shipped_catalog_states_its_evidence_and_passes_the_guard():
    assert E.check_catalog(ENVS) == []
    assert all(e["evidence_state"] in E.EVIDENCE_STATES and e["origin"] == "shipped" and isinstance(e["domains"], list) for e in ENVS.values())


def test_a_shipped_entry_without_an_evidence_state_or_with_a_made_up_one_is_refused():
    bad = {"x": {**copy.deepcopy(ENVS["pi"]), "id": "x", "evidence_state": None}}
    del bad["x"]["evidence_state"]
    assert any("must state its evidence_state" in p for p in E.check_catalog(bad))
    bad["x"]["evidence_state"] = "great"
    assert any("evidence_state must be one of" in p for p in E.check_catalog(bad))


def test_what_we_install_must_be_permissive_pinned_and_hashed():
    e = copy.deepcopy(ENVS["opencode"])
    assert E.check_catalog({"opencode": e}) == []
    gpl = {**e, "license": {"spdx": "GPL-3.0-only"}}
    assert any("the license must be one of" in p for p in E.check_catalog({"opencode": gpl}))
    nohash = copy.deepcopy(e)
    nohash["upstream"]["assets"]["darwin-arm64"].pop("sha256")
    assert any("SHA-256" in p for p in E.check_catalog({"opencode": nohash}))
    nopin = copy.deepcopy(e)
    nopin["upstream"].pop("commit"); nopin.pop("pin", None)
    assert any("needs a pin" in p for p in E.check_catalog({"opencode": nopin}))
    closed_vendor = copy.deepcopy(ENVS["claude-code"])  # a closed product the owner already has is never installed by us, so it is not held to the license list
    assert E.check_catalog({"claude-code": closed_vendor}) == []


def test_a_researched_lead_passes_only_as_a_hypothesis_with_a_fetched_source_and_never_runs():
    lead = {**LEAD, "origin": "researched"}
    assert E.check_catalog({"finlead": lead}) == []
    assert not E.is_runnable(lead)
    for patch, needle in [({"adapter": {"kind": "cli"}}, "has no adapter"), ({"evidence_state": "verified"}, "only record a hypothesis"), ({"fetched": None}, "names its fetched"),
                          ({"retrieved": ""}, "names its retrieved"), ({"license": {"spdx": "AGPL-3.0-only"}}, "the license must be one of"), ({"source_url": "http://x"}, "must be https"),
                          ({"accesses": [{"id": "a", "kind": "local", "cost_class": "free"}]}, "cost class"), ({"domains": ["", "x"]}, "short tags")]:
        problems = E.check_catalog({"finlead": {**lead, **patch}})
        assert any(needle in p for p in problems), (patch, problems)


def test_the_loader_merges_leads_from_the_state_dir_and_rejects_the_unsafe_ones_one_by_one(tmp_path):
    d = write_lead(tmp_path, LEAD, {**LEAD, "id": "badlead", "license": {"spdx": "GPL-3.0-only"}}, {**LEAD, "id": "pi"}, {**LEAD, "id": "withrun", "adapter": {"kind": "cli"}})
    envs, rejected = E.load_catalog(extra_dir=d)
    assert envs["finlead"]["origin"] == "researched" and envs["pi"]["origin"] == "shipped" and envs["pi"] == E.load_environments()["pi"]
    assert set(rejected) == {"badlead", "pi", "withrun"} and "already exists in the shipped catalog" in rejected["pi"][0]
    assert "finlead" not in E.runnable(envs) and set(E.runnable(envs)) == {k for k, e in ENVS.items() if e.get("subtype") != "skill_pack"}  # a skill pack is files an environment reads, never dispatched; a lead is shown and never dispatched


def test_a_broken_researched_file_never_takes_the_shipped_catalog_down(tmp_path):
    (tmp_path / "environments.json").write_text("{not json")
    envs, rejected = E.load_catalog(extra_dir=tmp_path)
    assert set(envs) == set(ENVS) and "(file)" in rejected


# ---- evidence tiers and the explanation line --------------------------------------------------------------------------------------------------------------------------------

def doc(*entries, excluded=()):
    return {"entries": list(entries), "excluded_benchmarks": list(excluded)}


def pub(model="local-8b", env=None, tier=2, bench="bench-x", **kw):
    return {"id": "e", "applies": {"model": model, **({"environment": env} if env else {})}, "task_types": ["text"], "benchmark": bench, "result": "61%", "source": "Example Institute", "source_url": "https://example.org/r",
            "source_tier": tier, "retrieved": "2026-11-02", **kw}


def route_for(env="llamacpp-local", model="local-8b"):
    from envrec_coordinator.router.model import Route
    return Route(environment_id=env, subtype="local_model", model_id=model, access_id="local-hardware", machine_id="mac")


def test_the_most_specific_evidence_speaks_alone_and_vendor_numbers_never_count():
    r = route_for()
    assert EV.describe(r, "text", [], None, None, doc())["tier"] == "none"
    d = doc(pub(env=None), pub(env="llamacpp-local", tier=3))
    assert EV.describe(r, "text", [], None, None, d)["tier"] == "public_combination"  # the combination beats the model-only entry although its source tier is lower
    assert EV.describe(r, "text", [], None, None, doc(pub(env=None)))["tier"] == "public_model_only"
    assert EV.describe(r, "text", [], None, None, doc(pub(tier="vendor")))["tier"] == "none"
    assert EV.describe(r, "text", [], None, None, doc(pub(bench="humaneval"), excluded=[{"benchmark": "humaneval", "reason": "saturated"}]))["tier"] == "none"
    assert EV.describe(r, "text", [], None, None, doc(pub(model="another")))["tier"] == "none"
    measured = EV.describe(r, "text", [], {"suite_id": "text-v1", "measured_at": 1793577600.0}, (9, 10), d)
    assert measured["tier"] == "measured_here" and measured["text"] == "evidence: measured here 9/10, admission suite text-v1, 2026-11-02"


def test_the_decision_carries_the_line_evidence_tier_source_date():
    pol = policy(open_subtypes={"local_model"})
    r = Router(envs=ENVS, facts=DictFacts([machine()]), policy=pol, evidence=doc(pub(env="llamacpp-local")))
    d = r.route(task(ttype="text", sens="public", chat="local"))
    assert d.route.environment_id == "llamacpp-local"
    assert d.evidence["text"] == "evidence: public for the combination (source tier 2, bench-x: 61%), Example Institute, 2026-11-02"
    none = Router(envs=ENVS, facts=DictFacts([machine()]), policy=pol, evidence=doc()).route(task("t2", ttype="text", sens="public", chat="local"))
    assert none.evidence["tier"] == "none" and not any(s.step == "evidence_tier" for s in none.rule_trace)


def test_public_evidence_ranks_unmeasured_candidates_of_the_same_cost_class_and_never_a_measured_one():
    two_local = {**ENVS}
    pol = policy(open_subtypes={"local_model", "own_harness"})
    m = machine()
    ev = doc(pub(env="opencode"))  # only the OpenCode combination has a public result
    r = Router(envs=two_local, facts=DictFacts([m]), policy=pol, evidence=ev)
    d = r.route(task(ttype="text", sens="public", chat="local"))
    step = next(s for s in d.rule_trace if s.step == "evidence_tier")
    assert d.route.environment_id == "opencode" and d.evidence["tier"] == "public_combination" and step.values[d.route.key] == 1
    measured = Router(envs=two_local, facts=DictFacts([m], admission=dict([admission("text", "llamacpp-local", passed=9)])), policy=pol, evidence=ev).route(task("t3", ttype="text", sens="public", chat="local"))
    assert measured.route.environment_id == "llamacpp-local" and measured.evidence["tier"] == "measured_here"
    assert not any(s.step == "evidence_tier" for s in measured.rule_trace)  # a measurement is the last word: public data never competes with it


# ---- domains: preferred after the sensitivity filter, never before it ----------------------------------------------------------------------------------------------------------------

def test_a_domain_tag_prefers_the_tagged_environment_and_the_step_says_so():
    envs = {**ENVS, "pi": {**ENVS["pi"], "domains": ["legal"]}}
    pol = policy(open_subtypes={"local_model", "own_harness"})
    m = machine()
    r = Router(envs=envs, facts=DictFacts([m]), policy=pol, evidence=doc())
    plain = r.route(task("a", ttype="text", sens="public", chat="local"))
    tagged = Router(envs=envs, facts=DictFacts([m]), policy=pol, evidence=doc()).route(Task(**{**task("b", ttype="text", sens="public", chat="local").model_dump(), "domain": ["Legal"]}))
    step = next(s for s in tagged.rule_trace if s.step == "domain_fit")
    assert tagged.route.environment_id == "pi" and step.candidates_out == [tagged.route.key] and "legal" in step.note
    assert not any(s.step == "domain_fit" for s in plain.rule_trace)  # without a domain the trace is what it was
    none_tagged = Router(envs=ENVS, facts=DictFacts([m]), policy=pol, evidence=doc()).route(Task(**{**task("c", ttype="text", sens="public", chat="local").model_dump(), "domain": ["legal"]}))
    assert none_tagged.route.key == plain.route.key and "no environment is tagged" in next(s for s in none_tagged.rule_trace if s.step == "domain_fit").note


def test_the_sensitivity_filter_runs_before_the_domain_preference_so_confidential_never_sees_a_cloud_access():
    cloud_tagged = {**ENVS, "claude-code": {**ENVS["claude-code"], "domains": ["legal"]}}
    pol = policy(open_subtypes={"local_model", "own_harness", "provider"})
    m = machine()
    adm = [admission("text", "llamacpp-local", passed=9), admission("text", "claude-code", passed=10)]
    t = Task(**{**task("d", ttype="text", sens="confidential", chat="local").model_dump(), "domain": ["legal"]})
    d = Router(envs=cloud_tagged, facts=DictFacts([m], admission=dict(adm)), policy=pol, evidence=doc()).route(t)
    assert d.route.environment_id == "llamacpp-local"
    s0, dom = d.rule_trace[0], next(s for s in d.rule_trace if s.step == "domain_fit")
    assert s0.step == "sensitivity" and "claude-code|default|claude-code:plan|mac" not in s0.candidates_out and "claude-code|default|claude-code:plan|mac" not in dom.candidates_in


@pytest.mark.parametrize("bad", ["", "x" * 41, "a;b", "a\x00b"])
def test_a_domain_tag_is_a_short_plain_word(bad):
    with pytest.raises(ValueError):
        Task(task_id="t", task_type="text", sensitivity="public", entry_chat=EntryChat(kind="local", name="x"), created_by="OWNER", payload_ref="r", domain=[bad])

"""The `unmeasured` policy value (policy.json, `measured_first` by default): a candidate with a valid, admitted measurement beats one with none, whatever its cost class; when nothing is measured the
cheapest unmeasured one may win on public and internal tasks, flagged provisional; a confidential task never gets an unmeasured one (R3). SIMULATED-LOCAL: no process, no network."""
from __future__ import annotations

import json

import pytest

from envrec_coordinator.router import DictFacts, Policy, Refusal, Router
from envrec_net import envrun as E
from tests.test_router import ENVS, admission, machine, policy, task

LOCAL, API_ISH = "llamacpp-local", "claude-code"  # local < subscription in the cost order


def route(adm, sens="public", pol=None, ttype="text", chat="local"):
    r = Router(envs=ENVS, facts=DictFacts([machine()], admission=dict(adm)), policy=pol or policy(approved_providers_for_internal={"claude-code", "codex"}))
    return r.route(task(ttype=ttype, sens=sens, chat=chat))


def test_the_shipped_default_is_measured_first_and_it_is_data():
    assert json.loads((E.POLICY_FILE).read_text())["rule"]["unmeasured"] == "measured_first"
    assert Policy.defaults().unmeasured == "measured_first"


def test_a_measured_admitted_candidate_beats_a_cheaper_unmeasured_one():
    d = route([admission("text", API_ISH, passed=8)])  # the local one has no measurement
    assert d.route.environment_id == API_ISH and not d.provisional and d.evidence_label == "measured here 8/10"
    step = next(s for s in d.rule_trace if s.step == "admission_filter")
    assert step.values["llamacpp-local|qwen3-8b|local-hardware|mac"] == "unmeasured" and set(step.values.values()) == {"unmeasured"} and step.candidates_out == [d.route.key]
    assert "llamacpp-local|qwen3-8b|local-hardware|mac" not in step.candidates_out


def test_a_measured_local_one_still_wins_on_cost_class_among_measured_ones():
    d = route([admission("text", LOCAL, passed=9), admission("text", API_ISH, passed=10)])
    assert d.route.environment_id == LOCAL and not d.provisional


def test_a_failed_measurement_is_dropped_as_before_and_does_not_make_the_unmeasured_ones_lose():
    d = route([admission("text", LOCAL, passed=6)])  # measured and below 8/10: dropped; nothing else is measured, so the cheapest unmeasured one is next
    step = next(s for s in d.rule_trace if s.step == "admission_filter")
    assert step.values["llamacpp-local|qwen3-8b|local-hardware|mac"] == "not_admitted" and d.route.environment_id != LOCAL and d.provisional


def test_with_nothing_measured_the_cheapest_unmeasured_wins_and_is_flagged_provisional():
    d = route([])
    assert d.route.environment_id == LOCAL and d.provisional and d.evidence_label == "no data"
    assert next(s for s in d.rule_trace if s.step == "cost_class").values[d.route.key] == "local"


def test_a_confidential_task_never_gets_an_unmeasured_candidate_even_when_nothing_else_exists():
    out = route([], sens="confidential")
    assert isinstance(out, Refusal) and out.code == "provisional_only"
    d = route([admission("text", LOCAL, passed=9)], sens="confidential")
    assert d.route.environment_id == LOCAL and not d.provisional


def test_cost_first_keeps_the_older_behaviour_an_unmeasured_cheaper_candidate_still_wins():
    d = route([admission("text", API_ISH, passed=9)], pol=policy(unmeasured="cost_first"))
    assert d.route.environment_id == LOCAL and d.provisional


def test_a_policy_value_that_is_not_known_is_refused_when_the_policy_is_read():
    doc = json.loads(E.POLICY_FILE.read_text())
    doc["rule"]["unmeasured"] = "whatever"
    with pytest.raises(ValueError):
        Policy.defaults(doc)

"""The router (T4a): the written rule, sensitivity in code, escalation, the outbound filter. Pure: no network, no process; the catalog is the shipped one (skills/router-environments). Labels: SIMULATED-LOCAL
where a stand-in appears elsewhere; nothing here ran a real environment."""
from __future__ import annotations

import pytest

from envrec_coordinator.router import DictFacts, EntryChat, MachineFacts, Policy, Refusal, RouteDecision, Router, Task, filter_outbound
from envrec_coordinator.router.facts import admission_key
from envrec_net import envrun as E

ENVS = E.load_environments()


def machine(mid="mac", **kw) -> MachineFacts:
    base = dict(machine_id=mid, os_family="darwin", installed={"claude-code", "codex", "opencode", "muse-code", "pi"}, signed_in={"claude-code:plan", "codex:plan"}, env_names=set(),
                sandbox_backends=["docker"], model_endpoint="127.0.0.1:8080", model_id="qwen3-8b")
    base.update(kw)
    return MachineFacts(**base)


def policy(**kw) -> Policy:
    p = Policy.defaults()
    p.open_subtypes = {"local_model", "own_harness", "provider"}
    p.automated_use_cleared = {"claude-code", "codex", "muse-code"}
    p.approved_providers_for_internal = {"claude-code"}
    for k, v in kw.items():
        setattr(p, k, v)
    return p


def task(tid="t1", ttype="code", sens="public", chat="cloud", by="OWNER") -> Task:
    return Task(task_id=tid, task_type=ttype, sensitivity=sens, entry_chat=EntryChat(kind=chat, name="claude-desktop" if chat == "cloud" else "owner-panel"), created_by=by, payload_ref="ref-" + tid)


def admission(ttype, env, mach="mac", model="qwen3-8b", access=None, passed=9, **kw) -> tuple[str, dict]:
    access = access or {"llamacpp-local": "local-hardware", "opencode": "opencode:local", "pi": "pi:local", "claude-code": "claude-code:plan", "codex": "codex:plan"}[env]
    model = model if env in ("llamacpp-local", "opencode", "pi") else "default"
    key = f"admission.{ttype}.{mach}.{env}.{model}.{access}"
    return key, {"status": "tested", "graded_by": "code", "passed": passed, "of": 10, "identity": {}, **kw}


def router(machines=None, pol=None, adm=None, **kw) -> tuple[Router, list]:
    log = []
    facts = DictFacts(machines or [machine()], admission=dict(adm or []), **kw)
    return Router(envs=ENVS, facts=facts, policy=pol or policy(), log=lambda t, p, a: log.append((t, a, p))), log


# ---- sensitivity, in code ---------------------------------------------------------------------------------------------------------------------------------------------------------

def test_a_confidential_task_from_a_cloud_chat_is_refused_before_any_candidate_is_looked_at():
    r, log = router()
    out = r.route(task(sens="confidential", chat="cloud"))
    assert isinstance(out, Refusal) and out.code == "confidential_from_cloud_chat" and "owner panel or a local chat" in out.reason
    assert [(t, a) for t, a, _ in log] == [("route_refusal", "GUARD")]


def test_a_task_without_a_level_is_refused_and_no_level_is_guessed():
    r, _ = router()
    assert r.route(task(sens=None)).code == "sensitivity_missing"
    assert r.route(task("t2", sens="secret")).code == "sensitivity_missing"


def test_a_confidential_task_goes_only_to_a_local_environment_measured_here_on_a_paired_machine():
    adm = [admission("text", "llamacpp-local"), admission("text", "claude-code")]  # the provider is measured too: it must still not be chosen
    r, _ = router(adm=adm)
    d = r.route(task(ttype="text", sens="confidential", chat="local"))
    assert isinstance(d, RouteDecision) and d.route.environment_id == "llamacpp-local" and d.evidence_label == "measured here 9/10"
    assert d.rule_trace[0].step == "sensitivity" and any("cloud" == v for v in d.rule_trace[0].values.values())


def test_a_confidential_task_with_only_unmeasured_local_combinations_is_refused_as_provisional_only():
    r, _ = router()
    out = r.route(task(ttype="text", sens="confidential", chat="local"))
    assert out.code == "provisional_only" and "measured" in out.reason


def test_a_confidential_task_with_no_local_environment_at_all_is_refused_as_no_local_candidate():
    r, _ = router(machines=[machine(model_endpoint=None, installed={"claude-code"})], adm=[admission("text", "claude-code")])
    out = r.route(task(ttype="text", sens="confidential", chat="local"))
    assert out.code == "no_local_candidate" and out.details["dropped"]


def test_a_stale_admission_fact_is_not_a_measurement_so_confidential_is_refused():
    key, fact = admission("text", "llamacpp-local", identity={"model": "old-sha"})
    r, _ = router(adm=[(key, fact)], identity={"llamacpp-local|qwen3-8b|local-hardware|mac": {"model": "new-sha"}})
    assert r.route(task(ttype="text", sens="confidential", chat="local")).code == "provisional_only"


def test_a_measurement_not_graded_by_code_or_not_tested_does_not_count():
    for bad in ({"graded_by": "model"}, {"status": "hypothesis"}):
        key, fact = admission("text", "llamacpp-local", **bad)
        r, _ = router(adm=[(key, fact)])
        assert r.route(task(ttype="text", sens="confidential", chat="local")).code == "provisional_only", bad


def test_internal_tasks_use_a_provider_only_if_the_owner_approved_it():
    r, _ = router(machines=[machine(model_endpoint=None)], pol=policy(approved_providers_for_internal=set()))
    assert r.route(task(sens="internal")).code == "no_candidate"
    r, _ = router(machines=[machine(model_endpoint=None)], pol=policy(approved_providers_for_internal={"claude-code"}))
    assert r.route(task(sens="internal")).route.environment_id == "claude-code"


def test_a_machine_that_is_not_paired_is_never_a_candidate():
    r, _ = router(machines=[machine("stranger", paired=False)])
    out = r.route(task(ttype="text", sens="public"))
    assert out.code == "no_candidate" and set(out.details["codes"].values()) == {"not_paired"}


# ---- the written rule ------------------------------------------------------------------------------------------------------------------------------------------------------------

def test_cost_class_comes_first_local_before_api_before_subscription():
    r, _ = router()
    d = r.route(task(ttype="text"))
    assert d.route.subtype in ("local_model", "own_harness") and next(s for s in d.rule_trace if s.step == "cost_class").values[d.route.key] == "local"
    assert [s.step for s in d.rule_trace] == ["sensitivity", "admission_filter", "cost_class", "admission_rate", "time", "load", "tie_break"]


def test_with_only_providers_the_api_access_ranks_before_the_subscription_access():
    m = machine(model_endpoint=None, installed={"muse-code", "claude-code"}, signed_in={"claude-code:plan"}, env_names={"META_API_KEY"})
    r, _ = router(machines=[m], pol=policy(open_subtypes={"provider"}))
    d = r.route(task(ttype="text"))
    assert d.route.access_id == "muse-code:api-key"  # cost class api < subscription


def test_a_measured_rate_ranks_above_no_data_and_differences_inside_the_margin_are_a_tie():
    adm = [admission("code", "llamacpp-local", passed=10), admission("code", "opencode", passed=9), admission("code", "pi", passed=8)]
    r, _ = router(adm=adm, pol=policy(tie_margin=0.1))
    d = r.route(task())
    step = next(s for s in d.rule_trace if s.step == "admission_rate")
    assert set(step.candidates_out) == {"llamacpp-local|qwen3-8b|local-hardware|mac", "opencode|qwen3-8b|opencode:local|mac"} and step.margin == 0.1  # 10/10 and 9/10 tie, 8/10 does not
    r2, _ = router(adm=adm, pol=policy(tie_margin=0.0))
    assert r2.route(task()).route.environment_id == "llamacpp-local"


def test_a_combination_that_failed_the_admission_exam_is_not_approved_even_if_it_would_win():
    adm = [admission("code", "llamacpp-local", passed=3), admission("code", "opencode", passed=9)]
    r, _ = router(adm=adm)
    d = r.route(task())
    assert d.route.environment_id == "opencode" and d.rule_trace[1].values == {"llamacpp-local|qwen3-8b|local-hardware|mac": "not_admitted"}


def test_measured_time_then_fewer_tasks_then_the_shared_plan_quota_break_remaining_ties():
    m1, m2 = machine("a", model_endpoint=None, installed={"claude-code", "codex"}), machine("b", model_endpoint=None, installed={"claude-code", "codex"})
    pol = policy(open_subtypes={"provider"}, approved_providers_for_internal={"claude-code", "codex"})
    r, _ = router(machines=[m1, m2], pol=pol, times={"claude-code|default|claude-code:plan|a": 5.0, "claude-code|default|claude-code:plan|b": 5.0, "codex|default|codex:plan|a": 9.0, "codex|default|codex:plan|b": 9.0},
                  leases={"a": 2}, quotas={"anthropic-plan": 0, "openai-plan": 0})
    assert r.route(task("t1", sens="internal")).route.key == "claude-code|default|claude-code:plan|b"  # fastest, then the machine with fewer leases
    r, _ = router(machines=[m1, m2], pol=pol, quotas={"anthropic-plan": 7, "openai-plan": 1})
    d = r.route(task("t2", sens="internal"))
    assert d.route.environment_id == "codex" and next(s for s in d.rule_trace if s.step == "load").values[d.route.key] == 1  # environments of one plan share a counter


def test_the_decision_is_deterministic_and_recomputable_from_the_same_facts():
    a, _ = router()
    b, _ = router()
    assert a.route(task()).model_dump() == b.route(task()).model_dump()


def test_routing_the_same_task_twice_returns_the_same_decision_and_logs_it_once():
    r, log = router()
    d = r.route(task())
    assert r.route(task()) is d and [t for t, _, _ in log].count("route_decision") == 1


def test_the_decision_is_a_router_event_and_a_refusal_is_a_guard_event():
    r, log = router(machines=[machine(model_endpoint=None, installed=set())])
    r.route(task("bad", ttype="code"))
    r2, log2 = router()
    r2.route(task())
    assert ("route_refusal", "GUARD") in [(t, a) for t, a, _ in log] and ("route_decision", "ROUTER") in [(t, a) for t, a, _ in log2]


def test_the_tie_margin_default_is_the_visible_config_value_and_the_default_policy_is_conservative():
    p = Policy.defaults()
    assert p.tie_margin == 0.1 and p.open_subtypes == {"local_model"} and p.automated_use_cleared == set() and p.max_escalation == 2


def test_an_environment_whose_automated_use_the_owner_has_not_cleared_is_never_routed_automatically():
    m = machine(model_endpoint=None, installed={"claude-code"}, signed_in={"claude-code:plan"})
    r, _ = router(machines=[m], pol=policy(open_subtypes={"provider"}, automated_use_cleared=set()))
    out = r.route(task())
    assert out.code == "automated_use_unreviewed" and "terms" in out.reason


def test_codex_and_muse_code_run_only_inside_our_sandbox_for_every_task_type():
    m = machine(model_endpoint=None, installed={"codex", "muse-code"}, signed_in={"codex:plan", "muse-code:plan"}, sandbox_backends=[])
    r, _ = router(machines=[m], pol=policy(open_subtypes={"provider"}, automated_use_cleared={"codex", "muse-code"}))
    for ttype in ("code", "text"):
        out = r.route(task("c-" + ttype, ttype=ttype))
        assert out.code == "sandbox_missing", ttype  # codex's own sandbox is not trusted until we verify it, and no flag removes its shell tool: zero execution outside our sandbox
    m.sandbox_backends = ["docker"]
    r2, _ = router(machines=[m], pol=policy(open_subtypes={"provider"}, automated_use_cleared={"codex", "muse-code"}))
    assert r2.route(task("c2", ttype="text")).route.environment_id in ("codex", "muse-code") and r2.route(task("c3", ttype="code")).route.environment_id in ("codex", "muse-code")


def test_pi_never_runs_without_a_sandbox_backend_and_an_unknown_task_type_is_refused():
    m = machine(installed={"pi"}, sandbox_backends=[], model_endpoint="127.0.0.1:8080")
    r, _ = router(machines=[m], pol=policy(open_subtypes={"own_harness"}))
    assert r.route(task()).code == "sandbox_missing"
    assert r.route(task("u", ttype="translate")).code == "unknown_task_type"


def test_muse_code_has_two_accesses_a_plan_and_an_api_key_found_by_either_variable_name():
    muse = ENVS["muse-code"]
    key = next(a for a in muse["accesses"] if a["kind"] == "api_key")
    assert key["env_names_any"] == ["MODEL_API_KEY", "META_API_KEY"] and key["env_names"] == [] and [a["id"] for a in muse["accesses"]] == ["muse-code:plan", "muse-code:api-key"]
    assert muse["cloud"] is True and muse["automated_use"] == "to review"
    base = dict(model_endpoint=None, installed={"muse-code"}, signed_in=set())
    pol = policy(open_subtypes={"provider"})
    r, _ = router(machines=[machine(env_names=set(), **base)], pol=pol)
    out = r.route(task("m0"))
    assert out.code == "no_candidate" and out.details["codes"]["muse-code@mac"] == "not_signed_in"  # presence only: no plan session and neither variable NAME exists
    for name in ("MODEL_API_KEY", "META_API_KEY"):
        r, _ = router(machines=[machine(env_names={name}, **base)], pol=pol)
        assert r.route(task("m-" + name)).route.access_id == "muse-code:api-key", name  # either name enables it (names only, never values)


# ---- escalation ------------------------------------------------------------------------------------------------------------------------------------------------------------------

def _fail(r, d, grader="code"):
    return r.report_result(d.task_id, d.escalation_step, {"passed": False, "grader": grader, "evidence_ref": "ev"})


def test_a_failing_code_grader_escalates_to_the_next_candidate_by_the_rule_at_most_twice_then_stops():
    r, log = router()
    d0 = r.route(task())
    d1 = _fail(r, d0)
    d2 = _fail(r, d1)
    assert [d.escalation_step for d in (d0, d1, d2)] == [0, 1, 2] and len({d0.route.key, d1.route.key, d2.route.key}) == 3  # never the same combination twice
    out = _fail(r, d2)
    assert isinstance(out, Refusal) and out.code == "escalation_exhausted" and out.details["owner_notice"]["text_fallback"].startswith("Task t1 could not be completed")
    assert [t for t, _, _ in log].count("escalation") == 2 and [a for t, a, _ in log if t == "owner_notice"] == ["GUARD"]
    assert all(a == "ROUTER" for t, a, _ in log if t == "escalation")


def test_a_model_graded_result_can_neither_pass_nor_fail_a_task():
    r, _ = router()
    d = r.route(task())
    out = _fail(r, d, grader="model")
    assert out.code == "weak_grader"
    assert r.report_result(d.task_id, 0, {"passed": True, "grader": "human"}).code == "weak_grader"
    assert r.report_result(d.task_id, 0, {"passed": True, "grader": "code"}) is None  # code passes it: closed


def test_a_result_for_an_old_decision_is_ignored():
    r, _ = router()
    d0 = r.route(task())
    _fail(r, d0)
    assert r.report_result("t1", 0, {"passed": True, "grader": "code"}).code == "stale_decision"
    assert r.report_result("nope", 0, {"passed": True, "grader": "code"}).code == "stale_decision"


def test_escalation_never_widens_sensitivity_and_ends_when_no_allowed_candidate_is_left():
    adm = [admission("text", "llamacpp-local"), admission("text", "claude-code")]
    r, _ = router(adm=adm)
    d0 = r.route(task(ttype="text", sens="confidential", chat="local"))
    assert d0.route.environment_id == "llamacpp-local"
    out = _fail(r, d0)  # the provider is measured but is cloud: it is not offered, so there is nothing to escalate to
    assert isinstance(out, Refusal) and out.code == "escalation_exhausted" and "claude-code|default|claude-code:plan|mac" not in out.details["tried"]


# ---- the outbound filter ------------------------------------------------------------------------------------------------------------------------------------------------------

INTENT = {"kind": "show_data", "run_id": "r1", "actor": "GUARD", "sensitivity": "confidential", "payload": {"task_id": "t9", "state": "done", "machine": "lenovo", "result": "SECRET TEXT", "files": ["a.py"]}, "text_fallback": "SECRET TEXT here"}


def test_for_a_cloud_chat_a_confidential_payload_becomes_a_summary_with_no_content():
    out = filter_outbound(INTENT, {"kind": "cloud", "name": "claude-desktop"})
    assert out["payload"] == {"task_id": "t9", "state": "done", "machine": "lenovo"} and out["redacted"] is True and out["sensitivity"] == "confidential"
    assert "SECRET" not in repr(out) and out["text_fallback"] == "Task t9 is done on lenovo. Its content stays on your computers."


def test_for_a_local_chat_the_intent_is_unchanged_and_public_is_unchanged_everywhere():
    assert filter_outbound(INTENT, {"kind": "local", "name": "open-webui"}) == INTENT
    pub = {**INTENT, "sensitivity": "public"}
    assert filter_outbound(pub, {"kind": "cloud", "name": "x"}) == pub


def test_an_unknown_level_or_entry_kind_is_treated_as_confidential_and_cloud():
    assert "SECRET" not in repr(filter_outbound({**INTENT, "sensitivity": "weird"}, {"kind": "cloud"}))
    assert "SECRET" not in repr(filter_outbound(INTENT, {"kind": "mystery"})) and "SECRET" not in repr(filter_outbound(INTENT, None))


def test_internal_to_a_cloud_chat_drops_content_keys_and_caps_size():
    out = filter_outbound({**INTENT, "sensitivity": "internal", "payload": {"task_id": "t", "result": "x", "note": "y" * 900}}, {"kind": "cloud"}, outbound_policy=Policy.defaults().outbound)
    assert "result" not in out["payload"] and len(out["payload"]["note"]) == 500 and out["redacted"] is True


def test_the_router_filter_method_uses_the_owner_policy():
    r, _ = router()
    assert "SECRET" not in repr(r.filter_outbound(INTENT, {"kind": "cloud"}))

"""T4a x T4c wiring (integration): `run_tests` through the sandbox entry, the router reading admission facts with a fresh identity, and the options list relabelled from fresh facts. SIMULATED-LOCAL:
the sandbox entry `sbx.run_exam_code` is replaced by a stand-in where noted; the end-to-end run in Docker is LINUX-CONTAINER and skipped when Docker is not ready."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_admission import Env, combo  # noqa: E402
from test_router import ENVS, machine, policy, task  # noqa: E402

from envrec.skills import sbx
from envrec_coordinator import options as O
from envrec_coordinator.admission import router_bridge as B
from envrec_coordinator.admission.exam import run_exam
from envrec_coordinator.admission.facts import AdmissionFacts, Combo
from envrec_coordinator.admission.suite import load_suite
from envrec_coordinator.router import Refusal, Router
from envrec_coordinator.router.facts import admission_key
from envrec_coordinator.router.outputs import MemoryOutputs
from envrec_net.log import EventLog

SHA, VER = "aa" * 32, "b1"


@pytest.fixture
def store(tmp_path):
    log = EventLog(tmp_path / "log")
    yield AdmissionFacts(log, clock=lambda: 1000.0)
    log.close()


def graded_router(spec, **kw):
    from envrec_coordinator.router import DictFacts
    r = Router(envs=ENVS, facts=DictFacts([machine()]), policy=policy(), outputs=MemoryOutputs(), **kw)
    ref = r.outputs.put("g", "public", {"output_text": "```python\ndef f():\n    return 1\n```"})
    r.set_grader("g", spec)
    return r, ref


SPEC = {"kind": "run_tests", "value": "check(f(), 1)", "n_checks": 1}


def test_run_tests_answers_sandbox_missing_when_no_sandbox_is_ready_and_never_calls_the_entry(monkeypatch):
    calls = []
    monkeypatch.setattr(sbx, "run_exam_code", lambda *a, **k: calls.append(a) or {})
    monkeypatch.setattr(sbx, "docker_ready", lambda image=sbx.IMAGE: False)
    r, ref = graded_router(SPEC, sandbox_ready=B.sandbox_ready, sandbox_runner=B.sandbox_runner)
    out = r.grade("g", ref)
    assert isinstance(out, Refusal) and out.code == "sandbox_missing" and calls == []


def test_run_tests_runs_through_the_exam_entry_when_a_sandbox_is_ready(monkeypatch):
    seen = []
    monkeypatch.setattr(sbx, "docker_ready", lambda image=sbx.IMAGE: True)
    monkeypatch.setattr(sbx, "run_exam_code", lambda sol, checks, n, timeout_s: seen.append((sol, checks, n)) or {"ok": True, "kind": "passed", "checked": n, "detail": ""})
    r, ref = graded_router(SPEC, sandbox_ready=B.sandbox_ready, sandbox_runner=B.sandbox_runner)
    res = r.grade("g", ref)
    assert (res.passed, res.grader) == (True, "code") and seen == [("def f():\n    return 1", "check(f(), 1)", 1)]
    monkeypatch.setattr(sbx, "run_exam_code", lambda sol, checks, n, timeout_s: {"ok": False, "kind": "wrong_answer", "checked": 0, "detail": ""})
    assert r.grade("g", ref).passed is False


def test_a_sandbox_that_goes_away_between_the_check_and_the_run_is_sandbox_missing_not_a_crash(monkeypatch):
    monkeypatch.setattr(sbx, "docker_ready", lambda image=sbx.IMAGE: True)

    def gone(*a, **k):
        raise sbx.SandboxUnavailable("gone")
    monkeypatch.setattr(sbx, "run_exam_code", gone)
    r, ref = graded_router(SPEC, sandbox_ready=B.sandbox_ready, sandbox_runner=B.sandbox_runner)
    out = r.grade("g", ref)
    assert isinstance(out, Refusal) and out.code == "sandbox_missing"


def test_a_spec_without_n_checks_or_an_output_without_code_fails_closed_without_running(monkeypatch):
    monkeypatch.setattr(sbx, "docker_ready", lambda image=sbx.IMAGE: True)
    monkeypatch.setattr(sbx, "run_exam_code", lambda *a, **k: pytest.fail("must not run"))
    r, ref = graded_router({"kind": "run_tests", "value": "assert f() == 1"}, sandbox_ready=B.sandbox_ready, sandbox_runner=B.sandbox_runner)
    assert r.grade("g", ref).passed is False


def _facts(store, current):
    return B.RouterAdmissionFacts([machine()], store, current)


def _measure(store, **kw):
    s = load_suite("code")
    c = combo(task_type="code", machine_id="mac", environment_id="llamacpp-local", model_id="qwen3-8b", access_id="local-hardware", model_sha256=SHA, environment_version=VER, **kw)
    run_exam(c, s, Env(s), store, code_runner=lambda sol, checks, n, t: {"ok": True, "kind": "passed", "checked": n, "detail": ""})
    return c


def test_the_router_sees_an_admission_only_while_the_model_environment_and_suite_are_the_ones_measured(store):
    c = _measure(store)
    route_key = "|".join((c.environment_id, c.model_id, c.access_id, c.machine_id))
    fresh = _facts(store, lambda k: (SHA, VER))
    fact = fresh.admission(admission_key("code", type("R", (), {"machine_id": "mac", "environment_id": "llamacpp-local", "model_id": "qwen3-8b", "access_id": "local-hardware"})()))
    assert fact["status"] == "tested" and fresh.identity(route_key) == fact["identity"]
    for now in ((("bb" * 32), VER), (SHA, "b2")):  # the model file or the environment changed since the measurement
        assert _facts(store, lambda k, now=now: now).identity(route_key) != fact["identity"]
    assert fresh.identity("pi|default|pi:local|mac") == {}  # nothing measured for that route


def test_the_router_ranks_a_confidential_task_onto_a_combination_measured_here_and_drops_it_when_stale(store):
    c = _measure(store)
    facts = B.RouterAdmissionFacts([machine()], store, lambda k: (SHA, VER))
    r = Router(envs=ENVS, facts=facts, policy=policy())
    out = r.route(task("c1", "code", "confidential", chat="local"))
    assert not isinstance(out, Refusal) and out.route.environment_id == "llamacpp-local"
    stale = B.RouterAdmissionFacts([machine()], store, lambda k: (SHA, "b2"))
    out2 = Router(envs=ENVS, facts=stale, policy=policy()).route(task("c2", "code", "confidential", chat="local"))
    assert isinstance(out2, Refusal)  # provisional is never enough for confidential


def test_the_options_list_says_measured_here_only_from_fresh_facts(store):
    s = load_suite("code")
    c = _measure(store)
    mapping = {"opt-now": c, "opt-old": Combo(**{**c.__dict__, "environment_version": "b0"})}
    lookup = B.option_lookup(store, lambda o: mapping.get(o["id"]))
    base = {"kind": "local", "origin": "catalog_runtime", "cost_class": "free_local", "params_b": 8.0}
    opts = O.relabel_measured([{**base, "id": i, "title": i} for i in ("opt-now", "opt-old", "opt-none")], lookup)
    by = {o["id"]: o for o in opts}
    assert by["opt-now"]["measured"]["pass_rate"] == 1.0 and by["opt-now"]["admission_label"].startswith("measured here")
    assert "measured" not in by["opt-old"] and by["opt-old"]["admission_state"] == "stale"
    assert "admission_state" not in by["opt-none"] and s.hash


def test_the_bridge_is_the_only_place_the_router_and_admission_meet():
    root = Path(__file__).resolve().parents[1] / "src" / "envrec_coordinator"
    for f in (root / "router").glob("*.py"):
        assert "envrec_coordinator.admission" not in f.read_text(encoding="utf-8"), f.name  # the router stays independent of admission


@pytest.mark.skipif(not sbx.docker_ready(), reason="Docker (or the sandbox image) is not ready")
def test_run_tests_in_the_real_sandbox_container_passes_a_right_solution_and_fails_a_wrong_one():  # LINUX-CONTAINER
    r, ref = graded_router(SPEC, sandbox_ready=B.sandbox_ready, sandbox_runner=B.sandbox_runner)
    assert r.grade("g", ref).passed is True
    bad = r.outputs.put("g", "public", {"output_text": "```python\ndef f():\n    return 2\n```"})
    assert r.grade("g", bad).passed is False

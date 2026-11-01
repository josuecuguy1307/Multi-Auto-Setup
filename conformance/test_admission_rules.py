"""ADMISSION (A1 and the checks around it): what counts as passing, and who may start an exam (docs/RECOMMENDATION_SPEC.md section 8, docs/ROUTER_API.md section 4).
Every check also runs on a fixture that breaks the rule and must fail there, so a passing check is never vacuous. Stand-in environments only (SIMULATED-LOCAL)."""
from __future__ import annotations

import ast
import textwrap

import pytest
import yaml

import netrules
from conftest import ROOT

from envrec_coordinator.admission.decision import AdmissionDesk, Refusal
from envrec_coordinator.admission.exam import ExamAborted, Reply, run_exam
from envrec_coordinator.admission.facts import AdmissionFacts, Combo, ExamOutcome, TaskResult, WeakGraderRefused, allows, view_of
from envrec_coordinator.admission.suite import DATA_ROOT, load_suite, parse_suite
from envrec_net.log import EventLog

ADM = ROOT / "src" / "envrec_coordinator" / "admission"
COMBO = Combo("text", "m1", "env", "model", "local", "aa" * 32, "v1")


class Stand:
    def __init__(self, suite):
        self.ref, self.asked = {t.id: t.reference for t in suite.tasks}, []

    def ask(self, prompt, *, task_id, timeout_s):
        self.asked.append(task_id)
        return Reply(self.ref[task_id], 1.0)


@pytest.fixture
def facts(tmp_path):
    log = EventLog(tmp_path / "log")
    yield AdmissionFacts(log, clock=lambda: 1.0)
    log.close()


# ---- A1: only code-graded tasks count toward the threshold ----------------------------------------------------------------------------------------

def counted_passes(counter) -> int:
    """Ten tasks that all pass: eight graded by a model, two by code. A correct counter reports at most 2."""
    results = [TaskResult(f"t{i}", True, "model" if i < 8 else "exact", "passed", 1.0) for i in range(10)]
    return counter(results)


def real_counter(results) -> int:
    return ExamOutcome(COMBO, "s", "sha256:x", 8, 10, tuple(results), None).passed


def test_A1_eight_model_graded_and_two_code_graded_tasks_cannot_reach_the_threshold():
    assert counted_passes(real_counter) == 2 < 8


def test_A1_fails_on_a_counter_that_counts_a_model_graded_task():
    assert counted_passes(lambda rs: sum(r.passed for r in rs)) == 10  # the fixture counter: this is what the check above would reject


def test_A1_a_model_graded_result_is_never_stored_as_an_admission(facts):
    weak = ExamOutcome(COMBO, "s", "sha256:x", 8, 10, tuple(TaskResult(f"t{i}", True, "model", "passed", 1.0) for i in range(10)), None)
    with pytest.raises(WeakGraderRefused):
        facts.write(weak)
    assert facts.current(COMBO.key) is None


def test_A1_a_suite_cannot_load_a_grader_that_is_neither_code_nor_a_declared_weak_one():
    d = yaml.safe_load((DATA_ROOT / "text-v1" / "suite.yaml").read_text())
    d["tasks"][0]["grader"] = {"kind": "gpt_says_ok"}
    with pytest.raises(Exception):
        parse_suite(d)


# ---- A2: a result is valid only for the suite hash it was measured with --------------------------------------------------------------------------

def reuses_after_change(reader, facts) -> bool:
    """True when `reader` still calls the fact admitted after the suite changed."""
    s = load_suite("text")
    run_exam(COMBO, s, Stand(s), facts)
    d = yaml.safe_load((DATA_ROOT / "text-v1" / "suite.yaml").read_text())
    d["tasks"][0]["prompt"] += " Changed."
    return bool(reader(facts.current(COMBO.key), parse_suite(d)))


def test_A2_an_admission_fact_is_not_reused_after_the_suite_hash_changed(facts):
    real = lambda fact, suite: view_of(fact, COMBO, suite, 8).admitted  # noqa: E731
    assert reuses_after_change(real, facts) is False


def test_A2_fails_on_a_reader_that_ignores_the_suite_hash(facts):
    assert reuses_after_change(lambda fact, suite: fact["passed"] >= fact["threshold"], facts) is True


# ---- R3 hook: confidential only to what was measured here ----------------------------------------------------------------------------------

def confidential_goes_to_unmeasured(gate, facts) -> bool:
    s = load_suite("text")
    return bool(gate("confidential", facts.view(COMBO, s, 8)))  # nothing was measured for this combination


def test_A3_confidential_is_not_routed_to_an_unmeasured_or_stale_combination(facts):
    assert confidential_goes_to_unmeasured(lambda sens, view: allows(sens, view)[0], facts) is False
    s = load_suite("text")
    run_exam(COMBO, s, Stand(s), facts)
    stale = Combo("text", "m1", "env", "model", "local", "aa" * 32, "v2")
    assert allows("confidential", facts.view(stale, s, 8))[0] is False and allows("confidential", facts.view(COMBO, s, 8))[0] is True


def test_A3_fails_on_a_gate_that_lets_confidential_through_without_a_measurement(facts):
    assert confidential_goes_to_unmeasured(lambda sens, view: True, facts) is True


# ---- A4: an exam starts only after an OWNER decision, and only code behind that lookup runs it ---------------------------------------------------

def start_order_violations(source: str) -> list[str]:
    """In `start`, the approval lookup must come before the call that runs the exam."""
    fn = next(n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.FunctionDef) and n.name == "start")
    def first(name):
        return next((i for i, st in enumerate(fn.body) if any(isinstance(c, ast.Call) and name in ast.unparse(c.func) for c in ast.walk(st))), None)
    look, run = first("_approval"), first("run_exam")
    if look is None:
        return ["start never looks the owner's approval up"]
    if run is not None and run < look:
        return ["start runs the exam before it looks the owner's approval up"]
    return []


def test_A4_start_looks_the_owner_approval_up_before_the_exam_runs():
    assert start_order_violations((ADM / "decision.py").read_text()) == []


def test_A4_fails_on_a_start_that_runs_first_or_never_looks():
    run_first = "class D:\n    def start(self):\n        run_exam()\n        self._approval()\n"
    no_look = "class D:\n    def start(self):\n        run_exam()\n"
    assert start_order_violations(run_first) and start_order_violations(no_look)


def test_A4_the_exam_is_run_from_the_desk_and_nowhere_else():
    users = [h for h in netrules.references(ROOT / "src", "run_exam", except_file="decision.py") if not h.split(":")[0].endswith("exam.py")]
    assert users == [], users


def test_A4_fails_when_some_other_module_runs_the_exam(tmp_path):
    (tmp_path / "side.py").write_text("from x import run_exam\n\ndef go():\n    return run_exam()\n")
    assert netrules.references(tmp_path, "run_exam", except_file="decision.py")


def test_A4_nothing_but_a_human_gesture_gets_an_exam_started(facts):
    s = load_suite("text")
    d = AdmissionDesk(facts, sandbox_ready=lambda: True)
    p = d.propose(COMBO, s)
    for kw in (dict(by="owner"), dict(by="owner", gesture="chat"), dict(by="claude-code", gesture="panel")):
        assert d.answer(p["id"], "approve", **kw) is False
    env = Stand(s)
    with pytest.raises(Refusal):
        d.start(COMBO, s, env)
    assert env.asked == [] and facts.current(COMBO.key) is None


# ---- A5: model-written code never runs without the sandbox ------------------------------------------------------------------------------------

def test_A5_no_dynamic_execution_and_no_process_start_exists_in_the_admission_package():
    assert netrules.dynamic_exec_sites(ADM) == []
    for f in ADM.glob("*.py"):
        mods = {(n.module or "") if isinstance(n, ast.ImportFrom) else a.name for n in ast.walk(ast.parse(f.read_text())) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in getattr(n, "names", [None])}
        assert not {m for m in mods if m.split(".")[0] in ("subprocess", "os", "runpy", "importlib")}, (f.name, mods)


def test_A5_fails_on_a_grader_that_executes_a_reply_itself(tmp_path):
    (tmp_path / "g.py").write_text("def grade(reply):\n    exec(compile(reply, 'x', 'exec'), {})\n")
    assert netrules.dynamic_exec_sites(tmp_path)


def test_A5_a_code_exam_without_a_sandbox_asks_the_environment_nothing_and_stores_nothing(facts):
    s = load_suite("code")
    env = Stand(s)
    with pytest.raises(ExamAborted) as e:
        run_exam(Combo("code", "m1", "env", "model", "local"), s, env, facts, sandbox_ready=lambda: False)
    assert e.value.reason == "sandbox_missing" and env.asked == [] and facts.current("admission.code.m1.env.model.local") is None

"""The admission exam: suites, hashing, graders, threshold, early stop, stale detection, the owner decision, the relabel of options. SIMULATED-LOCAL with stand-in environments; the sandbox tests run
in real Docker (a Linux container) and are skipped when Docker is not ready."""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest
import yaml

from envrec.skills import sbx
from envrec_coordinator import options as O
from envrec_coordinator.admission import strings as S
from envrec_coordinator.admission.decision import OWNER_GESTURES, AdmissionDesk, Refusal, admission_id
from envrec_coordinator.admission.exam import ExamAborted, Reply, RunnerAborted, early_stop, run_exam
from envrec_coordinator.admission.facts import AdmissionFacts, Combo, ExamOutcome, TaskResult, WeakGraderRefused, allows, fact_value, identity_of, view_of
from envrec_coordinator.admission.graders import SandboxMissing, extract_code, grade
from envrec_coordinator.admission.suite import DATA_ROOT, SuiteRefused, load_suite, parse_suite
from envrec_net.log import EventLog

docker = pytest.mark.skipif(not sbx.docker_ready(), reason="Docker (or the sandbox image) is not ready")


def doc_of(task_type: str) -> dict:
    return yaml.safe_load((DATA_ROOT / f"{task_type}-v1" / "suite.yaml").read_text(encoding="utf-8"))


@pytest.fixture
def facts(tmp_path):
    log = EventLog(tmp_path / "log")
    yield AdmissionFacts(log, clock=lambda: 1000.0)
    log.close()


def combo(**kw) -> Combo:
    base = dict(task_type="text", machine_id="m1", environment_id="llamacpp", model_id="qwen3", access_id="local", model_sha256="aa" * 32, environment_version="b1")
    return Combo(**{**base, **kw})


class Env:
    """A stand-in environment: answers each task with its reference, except the tasks named in `wrong`, which get a wrong answer."""

    def __init__(self, suite, wrong=(), abort_after=None, slow=()):
        self.ref = {t.id: t.reference for t in suite.tasks}
        self.wrong, self.abort_after, self.slow, self.asked = set(wrong), abort_after, set(slow), []

    def ask(self, prompt, *, task_id, timeout_s):
        if self.abort_after is not None and len(self.asked) >= self.abort_after:
            raise RunnerAborted("runner_unavailable")
        self.asked.append(task_id)
        if task_id in self.slow:
            return Reply("", timeout_s, timed_out=True)
        text = "I cannot do that" if task_id in self.wrong else self.ref[task_id]
        return Reply("```python\n" + text + "\n```" if task_id.startswith("code") else text, 1.5)


def ref_code_runner(suite):
    refs = {t.checks: t.reference.strip() for t in suite.tasks if t.checks}

    def run(solution, checks, n, timeout_s):
        ok = solution.strip() == refs[checks]
        return {"ok": ok, "kind": "passed" if ok else "wrong_answer", "checked": n if ok else 0, "detail": ""}
    return run


# ---- suites: data, hash, refusals ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("tt", ["code", "text"])
def test_each_suite_has_ten_code_graded_tasks_with_a_named_license(tt):
    s = load_suite(tt)
    assert (s.task_type, len(s.tasks), s.counted) == (tt, 10, 10)
    assert all(t.counts and t.source["license"] == "project license (pending)" for t in s.tasks)


def test_the_suite_hash_is_stable_and_changes_with_any_character_of_a_task():
    a, b = load_suite("code"), load_suite("code")
    assert a.hash == b.hash and re.fullmatch(r"sha256:[0-9a-f]{64}", a.hash)
    for edit in (lambda d: d["tasks"][0].__setitem__("prompt", d["tasks"][0]["prompt"] + " "), lambda d: d["tasks"][3].__setitem__("checks", d["tasks"][3]["checks"].replace("2", "3", 1)),
                 lambda d: d["tasks"].pop(), lambda d: d.__setitem__("preamble", d["preamble"] + ".")):
        doc = copy.deepcopy(doc_of("code"))
        edit(doc)
        assert parse_suite(doc).hash != a.hash


def test_a_task_type_without_a_suite_is_refused_not_guessed(tmp_path):
    with pytest.raises(SuiteRefused) as e:
        load_suite("legal", root=DATA_ROOT)
    assert e.value.code == "suite_missing"


def test_a_task_whose_license_is_not_verified_is_not_loaded():
    doc = copy.deepcopy(doc_of("text"))
    doc["tasks"][0]["source"]["license"] = "MIT (said by a README)"
    with pytest.raises(SuiteRefused) as e:
        parse_suite(doc)
    assert e.value.code == "license_unverified"


def test_a_grader_that_is_not_a_known_one_is_refused():
    doc = copy.deepcopy(doc_of("text"))
    doc["tasks"][0]["grader"] = {"kind": "llm_judge"}
    with pytest.raises(SuiteRefused) as e:
        parse_suite(doc)
    assert e.value.code == "grader_unknown"


# ---- graders ----------------------------------------------------------------------------------------------------------------------------------

def test_every_text_reference_passes_its_grader_and_a_wrong_reply_fails_it():
    for t in load_suite("text").tasks:
        assert grade(t, t.reference).passed, t.id
        g = grade(t, "I am not sure.")
        assert not g.passed and g.reason in S.TASK_FAIL, (t.id, g)
        assert not grade(t, "").passed


def test_json_grader_is_exact_about_values_and_types():
    t = next(t for t in load_suite("text").tasks if t.id == "text-01-json-fields")
    assert grade(t, '```json\n' + t.reference + '\n```').passed
    assert not grade(t, t.reference.replace("34", '"34"')).passed
    assert not grade(t, t.reference.replace("1250", "1250.5")).passed
    assert not grade(t, t.reference.replace('"unit": "5B", ', "")).passed


def test_answer_line_needs_the_last_line_in_form_and_honours_a_tolerance():
    s = {t.id: t for t in load_suite("text").tasks}
    assert grade(s["text-02-word-problem"], "so\nANSWER: 68\n").passed
    assert not grade(s["text-02-word-problem"], "ANSWER: 68\nbut wait").passed
    assert not grade(s["text-02-word-problem"], "ANSWER: 67").passed
    assert grade(s["text-06-conversion"], "ANSWER: 22.22").passed and not grade(s["text-06-conversion"], "ANSWER: 22.5").passed


def test_unordered_lines_compare_as_a_set_but_extra_or_repeated_lines_fail():
    t = next(t for t in load_suite("text").tasks if t.id == "text-07-emails")
    assert grade(t, "d_e-f@sub.example.co.uk\nana@example.org\nbob.smith@mail.example.com").passed
    assert not grade(t, t.reference + "\nadmin@@bad").passed
    assert not grade(t, t.reference + "\nana@example.org").passed


def test_a_grader_that_is_not_code_cannot_grade_an_admission_task():
    t = parse_suite({**doc_of("text"), "tasks": [{**doc_of("text")["tasks"][0], "grader": {"kind": "model"}}]}).tasks[0]
    assert not t.counts
    with pytest.raises(ValueError):
        grade(t, "x")


def test_the_code_extractor_takes_the_fenced_block_or_the_whole_reply():
    assert extract_code("here\n```python\nx = 1\n```\nbye") == "x = 1"
    assert extract_code("x = 1") == "x = 1" and extract_code("   ") is None


def test_a_code_task_without_a_sandbox_raises_before_anything_runs():
    t = load_suite("code").tasks[0]
    def no_runner(*a):
        raise sbx.SandboxUnavailable("no docker")
    with pytest.raises(SandboxMissing):
        grade(t, "```python\n" + t.reference + "\n```", code_runner=no_runner)


@docker
def test_every_code_reference_passes_in_the_real_sandbox_and_a_stub_fails():  # LINUX-CONTAINER
    for t in load_suite("code").tasks:
        assert grade(t, "```python\n" + t.reference + "\n```").passed, t.id
        name = re.search(r"(?:def|class) (\w+)", t.reference).group(1)
        stub = grade(t, f"```python\ndef {name}(*a, **k):\n    return None\n```")
        assert not stub.passed and stub.reason in ("wrong_answer", "exception"), (t.id, stub)


@docker
def test_the_sandbox_reports_syntax_errors_timeouts_and_a_solution_that_swaps_the_checker():  # LINUX-CONTAINER
    t = load_suite("code").tasks[1]
    assert grade(t, "```python\ndef is_balanced(s:\n```").reason == "syntax_error"
    assert grade(t, "```python\nwhile True: pass\n```").reason == "timeout" if t.timeout_s <= 12 else True
    assert not grade(t, "```python\ncheck = lambda *a: None\nraises = check\ndef is_balanced(s): return None\n```").passed
    assert not grade(t, "```python\nimport os\nos._exit(0)\n```").passed


# ---- the exam: threshold, early stop, fact ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("passed,ran,stop_u,want", [(0, 3, True, "first_3"), (0, 3, False, "first_3"), (2, 3, True, None), (1, 4, True, "unreachable"), (1, 4, False, None), (2, 4, True, None),
                                                    (7, 10, True, None), (6, 9, True, "unreachable"), (7, 9, True, None), (0, 2, True, None)])
def test_early_stop_rules(passed, ran, stop_u, want):
    assert early_stop(passed, ran, 10, 8, stop_u) == want


def test_a_nine_of_ten_environment_is_admitted_and_the_fact_is_the_routers_shape(facts):
    s = load_suite("text")
    out = run_exam(combo(), s, Env(s, wrong={"text-05-iso-dates"}), facts)
    f = facts.current(combo().key)
    assert (out.passed, out.admitted, f["passed"], f["of"], f["threshold"], f["graded_by"], f["early_stop"]) == (9, True, 9, 10, 8, "code", False)
    assert f["label"] == "measured here: 9/10" and f["suite_hash"] == s.hash and f["tasks"] == [t.id for t in s.tasks]
    assert combo().key == "admission.text.m1.llamacpp.qwen3.local" and facts.view(combo(), s, 8).admitted


def test_the_fact_carries_the_status_and_identity_the_routers_reader_compares(facts):
    s = load_suite("text")
    run_exam(combo(), s, Env(s), facts)
    f = facts.current(combo().key)
    assert f["status"] == "tested" and f["identity"] == identity_of(combo(), s.hash) == {"model_sha256": "aa" * 32, "environment_version": "b1", "suite_hash": s.hash}
    assert identity_of(combo(model_sha256="bb" * 32), s.hash) != f["identity"]


def test_exactly_the_threshold_passes_and_one_below_does_not(facts):
    s = load_suite("text")
    assert run_exam(combo(), s, Env(s, wrong={"text-04-sorted-bullets", "text-05-iso-dates"}), facts).admitted
    out = run_exam(combo(model_id="b"), s, Env(s, wrong={"text-04-sorted-bullets", "text-05-iso-dates", "text-06-conversion"}), facts)
    assert (out.passed, len(out.results), out.admitted, out.early_stop) == (3, 6, False, "unreachable")  # the third failure makes 8 unreachable: it stops there


def test_an_environment_that_fails_the_first_three_is_stopped_and_labeled_so(facts):
    s = load_suite("text")
    env = Env(s, wrong={t.id for t in s.tasks[:3]})
    out = run_exam(combo(), s, env, facts)
    f = facts.current(combo().key)
    assert len(env.asked) == 3 and out.early_stop == "first_3" and f["early_stop"] and f["stop_reason"] == "first_3" and f["ran"] == 3 and f["outcome"] == "not_admitted"
    assert f["label"] == "measured here: 0/10, not admitted (3 of 10 tasks run; stopped early: the first 3 tasks failed; threshold 8/10)"


def test_the_unreachable_stop_is_on_by_default_and_visible_in_the_label(facts):
    s = load_suite("text")
    env = Env(s, wrong={s.tasks[3].id, s.tasks[5].id, s.tasks[6].id})
    run_exam(combo(), s, env, facts)
    f = facts.current(combo().key)
    assert len(env.asked) == 7 and f["stop_reason"] == "unreachable" and "the threshold can no longer be reached" in f["label"] and f["stop_unreachable"] is True


def test_a_timeout_of_the_environment_is_the_models_failure_but_an_unreachable_runner_is_not(facts):
    s = load_suite("text")
    run_exam(combo(), s, Env(s, slow={"text-02-word-problem"}), facts)
    assert [r for r in facts.current(combo().key)["results"] if r["task"] == "text-02-word-problem"][0]["reason"] == "timeout"
    env = Env(s, abort_after=4)
    with pytest.raises(ExamAborted) as e:
        run_exam(combo(model_id="other"), s, env, facts)
    assert (e.value.reason, e.value.ran) == ("runner_unavailable", 4)
    assert facts.current(combo(model_id="other").key) is None  # nothing is stored as a measurement
    assert [e.payload["reason"] for e in facts.log.events("admission_aborted")] == ["runner_unavailable"]


def test_no_sandbox_aborts_a_code_exam_before_the_environment_is_asked_anything(facts):
    s = load_suite("code")
    env = Env(s)
    with pytest.raises(ExamAborted) as e:
        run_exam(combo(task_type="code"), s, env, facts, sandbox_ready=lambda: False)
    assert e.value.reason == "sandbox_missing" and env.asked == [] and facts.current(combo(task_type="code").key) is None


def test_a_code_exam_with_a_stand_in_environment_end_to_end(facts):
    s = load_suite("code")
    out = run_exam(combo(task_type="code"), s, Env(s, wrong={"code-04-topwords"}), facts, code_runner=ref_code_runner(s), sandbox_ready=lambda: True)
    assert (out.passed, out.admitted) == (9, True)


def test_a_model_graded_result_cannot_be_stored_as_an_admission(facts):  # A1
    c = combo()
    weak = ExamOutcome(c, "text-v1", "sha256:x", 8, 10, tuple(TaskResult(f"t{i}", True, "model" if i < 8 else "exact", "passed", 1.0) for i in range(10)), None)
    with pytest.raises(WeakGraderRefused):
        fact_value(weak, 0.0)
    with pytest.raises(WeakGraderRefused):
        facts.write(weak)
    assert facts.current(c.key) is None


def test_a_suite_with_fewer_code_graded_tasks_than_the_mark_cannot_be_passed(facts):
    d = doc_of("text")
    d["tasks"] = [{**t, "grader": {"kind": "model"}} if i < 8 else t for i, t in enumerate(d["tasks"])]
    s = parse_suite(d)
    assert s.counted == 2
    with pytest.raises(ExamAborted) as e:
        run_exam(combo(), s, Env(s), facts)
    assert e.value.reason == "threshold_above_suite"


# ---- stale: never silently reused ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("change,word", [(dict(model_sha256="bb" * 32), "another model file"), (dict(environment_version="b2"), "another version of the environment")])
def test_a_changed_model_file_or_environment_makes_the_fact_stale(facts, change, word):
    s = load_suite("text")
    run_exam(combo(), s, Env(s), facts)
    v = facts.view(combo(**change), s, 8)
    assert v.state == "stale" and not v.admitted and word in v.label and v.label.startswith("stale: 10/10 was measured with")


def test_a_changed_suite_hash_or_threshold_makes_the_fact_stale_and_it_is_not_admitted(facts):
    s = load_suite("text")
    run_exam(combo(), s, Env(s), facts)
    d = copy.deepcopy(doc_of("text"))
    d["tasks"][0]["prompt"] += " Be brief."
    v = facts.view(combo(), parse_suite(d), 8)
    assert v.state == "stale" and v.stale == ("suite_hash",) and "an earlier version of the suite" in v.label and not v.admitted
    assert facts.view(combo(), s, 9).stale == ("threshold",) and facts.view(combo(), s, 8).admitted


def test_a_stale_fact_is_provisional_for_the_router_and_never_confidential(facts):
    s = load_suite("text")
    run_exam(combo(), s, Env(s), facts)
    stale = facts.view(combo(environment_version="b9"), s, 8)
    assert allows("confidential", stale) == (False, "provisional_only") and allows("public", stale) == (True, "provisional")


def test_confidential_only_reaches_a_combination_measured_here_and_admitted(facts):  # R3 hook
    s = load_suite("text")
    run_exam(combo(), s, Env(s), facts)
    run_exam(combo(model_id="weak"), s, Env(s, wrong={t.id for t in s.tasks[:3]}), facts)
    good, none_, bad = facts.view(combo(), s, 8), facts.view(combo(model_id="nothing"), s, 8), facts.view(combo(model_id="weak"), s, 8)
    assert allows("confidential", good) == (True, "measured_here")
    assert allows("confidential", none_) == (False, "provisional_only") and allows("internal", none_) == (True, "provisional")
    assert allows("public", bad) == (False, "not_admitted") and allows("confidential", bad)[0] is False


def test_the_view_of_an_unmeasured_combination_is_provisional(facts):
    assert view_of(None, combo(), load_suite("text"), 8).label == "provisional, not measured here"


# ---- the owner decision ----------------------------------------------------------------------------------------------------------------------

def desk(facts, **kw):
    sent = []
    d = AdmissionDesk(facts, outbound=lambda i: sent.append(i) or i, sandbox_ready=lambda: True, **kw)
    return d, sent


def test_the_proposal_shows_its_cost_in_english_and_never_starts_anything(facts):
    s = load_suite("text")
    d, sent = desk(facts)
    p = d.propose(combo(), s, tokens_per_s=20.0, machine_name="Lenovo")
    assert p["question"] == "approve_admission" and p["data"]["tasks"] == 10 and p["data"]["threshold"] == 8 and p["data"]["machine_id"] == "m1"
    assert "10 text tasks" in p["text"] and "Lenovo" in p["text"] and "about 1 min" in p["text"] and "Pass mark 8/10" in p["text"]
    assert sent[0]["kind"] == "confirm" and sent[0]["text_fallback"] == p["text"] and facts.log.events("admission_started") == []
    assert "not estimated" in desk(facts)[0].propose(combo(model_id="x"), s)["text"]


def test_a_bare_owner_answer_is_refused_and_the_decision_stays_open(facts):  # failing fixture: no gesture
    s = load_suite("text")
    d, sent = desk(facts)
    p = d.propose(combo(), s)
    assert d.answer(p["id"], "approve", by="owner") is False and d.answer(p["id"], "approve", by="owner", gesture="chat_tool") is False
    assert facts.log.events("admission_approved") == [] and len(facts.log.events("owner_authority_refused")) == 2 and d.pending()[0]["id"] == p["id"]
    assert sent[-1]["text_fallback"] == S.REFUSED_NO_GESTURE
    with pytest.raises(Refusal) as e:
        d.start(combo(), s, Env(s))
    assert e.value.code == "no_owner_decision"


def test_the_operators_answer_is_written_as_the_operators_and_does_not_count(facts):
    s = load_suite("text")
    d, _ = desk(facts)
    p = d.propose(combo(), s)
    assert d.answer(p["id"], "approve", by="claude-code", gesture="panel") is False
    assert [e.effective_tag for e in facts.log.events("admission_answer")] == ["CLAUDE-CODE"] and facts.log.events("admission_approved") == []
    with pytest.raises(Refusal):
        d.start(combo(), s, Env(s))


@pytest.mark.parametrize("gesture", OWNER_GESTURES)
def test_a_human_gesture_approves_and_the_start_runs_the_exam(facts, gesture):
    s = load_suite("text")
    d, sent = desk(facts)
    p = d.propose(combo(), s)
    assert d.answer(p["id"], "approve", gesture=gesture, channel="test") is True
    out = d.start(combo(), s, Env(s))
    assert out.admitted and [e.effective_tag for e in facts.log.events("admission_approved")] == ["OWNER"]
    assert [i["kind"] for i in sent] == ["confirm", "progress"] + ["progress"] * 10 + ["show_data"]
    assert sent[1]["text_fallback"].startswith("Measuring llamacpp with qwen3 on m1: 10 tasks") and sent[-1]["text_fallback"].endswith("measured here: 10/10. It can receive text tasks, including confidential ones.")


def test_an_approval_is_for_that_combination_suite_and_threshold_only_and_is_used_once(facts):
    s = load_suite("text")
    d, _ = desk(facts)
    d.answer(d.propose(combo(), s)["id"], "approve", gesture="panel")
    for other in (dict(combo=combo(model_id="other")), dict(threshold=9)):
        with pytest.raises(Refusal):
            d.start(other.get("combo", combo()), s, Env(s), threshold=other.get("threshold", 8))
    d.start(combo(), s, Env(s))
    with pytest.raises(Refusal):
        d.start(combo(), s, Env(s))
    assert admission_id(combo(), s, 8) != admission_id(combo(), s, 9)


def test_reject_closes_the_decision_and_an_aborted_start_keeps_the_approval(facts):
    s = load_suite("text")
    d, sent = desk(facts)
    p = d.propose(combo(), s)
    d.answer(p["id"], "reject", gesture="os_dialog")
    assert d.pending() == [] and [e.effective_tag for e in facts.log.events("admission_rejected")] == ["OWNER"]
    p = d.propose(combo(), s)
    d.answer(p["id"], "approve", gesture="panel")
    with pytest.raises(ExamAborted):
        d.start(combo(), s, Env(s, abort_after=0))
    assert sent[-1]["text_fallback"] == "The exam for llamacpp with qwen3 on m1 did not finish: the environment could not be reached. Nothing was stored, so it still counts as not measured."
    assert d.start(combo(), s, Env(s)).admitted  # the approval was not used up: nothing had been asked yet


def test_a_normal_run_is_model_guard_and_owner_only(facts):
    s = load_suite("text")
    d, _ = desk(facts)
    d.answer(d.propose(combo(), s)["id"], "approve", gesture="panel")
    d.start(combo(), s, Env(s))
    tags = {e.effective_tag for e in facts.log.events()}
    assert tags == {"MODEL", "GUARD", "OWNER"}
    assert {e.effective_tag for e in facts.log.events("admission_reply")} == {"MODEL"} and {e.effective_tag for e in facts.log.events("admission_task")} == {"GUARD"}


# ---- options relabel ---------------------------------------------------------------------------------------------------------------------------

def opt(i, **kw):
    return {"id": i, "kind": "local", "origin": "catalog_runtime", "cost_class": "free_local", "title": i, "params_b": 8.0, **kw}


def test_a_fresh_measurement_relabels_and_ranks_an_option_and_a_failed_one_leaves(facts):
    s = load_suite("text")
    run_exam(combo(model_id="good"), s, Env(s), facts)
    run_exam(combo(model_id="bad"), s, Env(s, wrong={t.id for t in s.tasks[:3]}), facts)
    run_exam(combo(model_id="old"), s, Env(s), facts)
    views = {"opt-good": facts.view(combo(model_id="good"), s, 8), "opt-bad": facts.view(combo(model_id="bad"), s, 8), "opt-old": facts.view(combo(model_id="old", environment_version="b2"), s, 8)}
    opts = O.relabel_measured([opt("opt-new"), opt("opt-good"), opt("opt-old")], lambda o: views.get(o["id"]))
    by = {o["id"]: o for o in opts}
    assert by["opt-good"]["measured"]["pass_rate"] == 1.0 and by["opt-good"]["admission_label"] == "measured here: 10/10"
    assert "measured" not in by["opt-old"] and by["opt-old"]["admission_state"] == "stale" and by["opt-old"]["admission_label"].startswith("stale:")
    assert [o["id"] for o in O.order_options(opts)][0] == "opt-good"
    assert views["opt-bad"].state == "not_admitted"


def test_every_owner_facing_admission_string_is_english_ascii_and_lives_in_one_module():
    for name in dir(S):
        v = getattr(S, name)
        for text in ([v] if isinstance(v, str) else list(v.values()) if isinstance(v, dict) and all(isinstance(x, str) for x in v.values()) else []):
            assert text.isascii(), (name, text)
    src = Path(__file__).resolve().parents[1] / "src" / "envrec_coordinator" / "admission"
    for f in src.glob("*.py"):
        assert f.read_text(encoding="utf-8").isascii(), f.name

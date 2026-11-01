"""`text_checks`: one generic, code-run grader for free text whose content cannot be compared whole (a summary): the figures it must carry, the strings it must not, how many lines, how many words.
No model grades anything. SIMULATED-LOCAL."""
from __future__ import annotations

import pytest

from envrec_coordinator.admission.graders import grade
from envrec_coordinator.admission.suite import SuiteRefused, Task, parse_suite

LICENSE = {"author": "project", "license": "project license (pending)"}


def task(**g) -> Task:
    return Task(id="t", title="t", prompt="p", grader={"kind": "text_checks", **g}, checks="", reference="r", timeout_s=10, source=LICENSE)


def test_every_figure_must_be_present_exactly_as_written():
    t = task(contains_all=["$4.2 billion", "12%"])
    assert grade(t, "Revenue was $4.2 billion, up 12%.").passed
    assert not grade(t, "Revenue was $4.2 billion, up 12 percent.").passed
    assert not grade(t, "Revenue was 4.2 billion, up 12%.").passed


def test_a_forbidden_string_fails_the_reply():
    t = task(contains_all=["Friday"], contains_none=["Monday"])
    assert grade(t, "Ship by Friday.").passed
    assert not grade(t, "Ship by Friday, not Monday.").passed


def test_line_count_counts_non_empty_lines_and_strips_bullets_only_when_asked():
    t = task(lines={"min": 3, "max": 3})
    assert grade(t, "- a\n- b\n\n- c\n").passed
    assert not grade(t, "- a\n- b\n").passed and not grade(t, "a\nb\nc\nd").passed
    s = task(contains_all=["alpha"], bullets="strip", lines={"min": 1, "max": 1})
    assert grade(s, "* alpha").passed


def test_max_words_counts_whitespace_separated_words():
    t = task(max_words=5)
    assert grade(t, "one two three four five").passed
    assert not grade(t, "one two three four five six").passed


def test_normalize_lower_makes_the_match_case_insensitive_and_the_default_is_exact_case():
    assert grade(task(contains_all=["Marta"], normalize="lower"), "marta will lead").passed
    assert not grade(task(contains_all=["Marta"]), "marta will lead").passed


def test_an_empty_reply_fails_and_a_grader_with_no_check_is_refused_at_load():
    assert not grade(task(contains_all=["x"]), "  ").passed
    doc = {"suite_id": "s", "task_type": "zz", "version": 1, "tasks": [{"id": "a", "prompt": "p", "reference": "r", "grader": {"kind": "text_checks"}, "source": LICENSE}]}
    with pytest.raises(SuiteRefused) as e:
        parse_suite(doc)
    assert e.value.code == "suite_invalid"

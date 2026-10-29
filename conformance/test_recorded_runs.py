"""The guard around the two checks that read recorded runs (V2, M2): where runs exist they run and cannot be skipped; where there are none they are skipped as "not measured", never passed."""
import sqlite3

import pytest

from recorded_runs import NOT_MEASURED, recorded_run_dbs, require_recorded_runs
from test_guards_verification import check_V2_recorded_runs_labeled
from test_skills_context_memory import check_M2_reused_skill_made_zero_model_calls


def make_run(root, name, events, nested=True):
    d = root / "runs" / name / ("x" if nested else "")
    d.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(d / "envrec.db")
    db.execute("create table events (type text)")
    db.executemany("insert into events values (?)", [(e,) for e in events])
    db.commit()
    db.close()


@pytest.mark.parametrize("layout", [True, False])
def test_with_a_recorded_run_the_guard_returns_it_and_never_skips(tmp_path, layout):
    make_run(tmp_path, "r1", ["model_call"], nested=layout)
    assert len(require_recorded_runs(tmp_path)) == 1  # no pytest.skip.Exception was raised


def test_without_runs_the_guard_skips_with_the_explicit_reason_and_the_checks_are_skipped_not_passed(tmp_path):
    assert recorded_run_dbs(tmp_path) == []
    for call in (require_recorded_runs, check_V2_recorded_runs_labeled, check_M2_reused_skill_made_zero_model_calls):
        with pytest.raises(pytest.skip.Exception) as exc:
            call(tmp_path)
        assert exc.value.msg == NOT_MEASURED == "not measured: no recorded runs in this checkout"


def test_an_empty_runs_folder_counts_as_no_runs(tmp_path):
    (tmp_path / "runs" / "empty" / "x").mkdir(parents=True)
    with pytest.raises(pytest.skip.Exception):
        require_recorded_runs(tmp_path)


def test_with_runs_the_m2_check_runs_exactly_as_before_and_can_fail(tmp_path):
    make_run(tmp_path, "no_reuse", ["model_call"])
    with pytest.raises(AssertionError, match="no recorded run reused a stored skill"):  # it ran (not skipped) and its own assertion fired
        check_M2_reused_skill_made_zero_model_calls(tmp_path)
    make_run(tmp_path, "reuse_with_calls", ["skill_reused", "model_call"])
    with pytest.raises(AssertionError):
        check_M2_reused_skill_made_zero_model_calls(tmp_path)


def test_with_runs_the_m2_check_passes_when_the_reused_skill_made_no_model_call(tmp_path):
    make_run(tmp_path, "reuse_clean", ["skill_reused"])
    check_M2_reused_skill_made_zero_model_calls(tmp_path)


def test_with_runs_the_v2_check_is_not_skipped(tmp_path):
    make_run(tmp_path, "r1", ["model_call"])
    try:
        check_V2_recorded_runs_labeled(tmp_path)
    except pytest.skip.Exception:
        pytest.fail("V2 was skipped although a recorded run exists")
    except AssertionError:
        pass  # it ran and judged this toy run: what matters here is that it was not skipped

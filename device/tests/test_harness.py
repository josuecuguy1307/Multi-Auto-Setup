"""The oracle itself is under test: it must be nearly perfect, because every later unit is optimised against it."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("device_accept", ROOT / "scripts" / "device_accept.py")
acc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(acc)


def _crit(tests, covers=("src",)):
    return {"id": "X-1", "phase": 1, "title": "t", "label": "REAL", "tests": list(tests), "covers": list(covers)}


@pytest.fixture
def world(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "unit_code.py").write_text("X = 1\n")
    return tmp_path


def _test(world, name, body):
    (world / name).write_text(body)
    return name


def test_passing_test_is_pass(world):
    r = acc.run_criterion(_crit([_test(world, "test_ok.py", "def test_a():\n    assert True\n")]), world)
    assert r["status"] == "PASS", r


def test_failing_test_is_fail(world):
    r = acc.run_criterion(_crit([_test(world, "test_bad.py", "def test_a():\n    assert False\n")]), world)
    assert r["status"] == "FAIL" and "failed" in r["reason"]


def test_skip_is_fail(world):
    body = "import pytest\ndef test_a():\n    pass\n@pytest.mark.skip\ndef test_b():\n    pass\n"
    r = acc.run_criterion(_crit([_test(world, "test_skip.py", body)]), world)
    assert r["status"] == "FAIL" and "skip" in r["reason"]


def test_missing_test_file_is_fail(world):
    r = acc.run_criterion(_crit(["test_nowhere.py"]), world)
    assert r["status"] == "FAIL" and "missing" in r["reason"]


def test_no_tests_listed_is_fail(world):
    assert acc.run_criterion(_crit([]), world)["status"] == "FAIL"


def test_zero_collected_is_fail(world):
    r = acc.run_criterion(_crit([_test(world, "test_empty.py", "X = 1\n")]), world)
    assert r["status"] == "FAIL"


def test_import_error_is_fail(world):
    r = acc.run_criterion(_crit([_test(world, "test_imp.py", "import no_such_module_xyz\ndef test_a():\n    pass\n")]), world)
    assert r["status"] == "FAIL"


def test_pass_goes_stale_when_covered_file_changes(world):
    c = _crit([_test(world, "test_ok.py", "def test_a():\n    assert True\n")])
    r = acc.run_criterion(c, world)
    doc = {"results": {"X-1": r}}
    assert acc.effective([c], doc, world)["X-1"]["status"] == "PASS"
    (world / "src" / "unit_code.py").write_text("X = 2\n")
    assert acc.effective([c], doc, world)["X-1"]["status"] == "STALE"


def test_pass_goes_stale_when_the_test_changes(world):
    name = _test(world, "test_ok.py", "def test_a():\n    assert True\n")
    c = _crit([name])
    doc = {"results": {"X-1": acc.run_criterion(c, world)}}
    (world / name).write_text("def test_a():\n    assert 1\n")
    assert acc.effective([c], doc, world)["X-1"]["status"] == "STALE"


def test_never_run_is_fail():
    c = _crit(["t.py"])
    assert acc.effective([c], {"results": {}})["X-1"]["status"] == "FAIL"


def test_hand_edit_is_detected(tmp_path):
    results = {"X-1": {"status": "FAIL"}}
    doc = {"results": results, "checksum": acc._checksum(results)}
    doc["results"]["X-1"]["status"] = "PASS"
    assert doc["checksum"] != acc._checksum(doc["results"])


def test_criteria_file_is_valid_and_covers_every_phase():
    crit = acc.load_criteria()
    assert {c["phase"] for c in crit} == {1, 2, 3, 4, 5, 6}
    assert all(c["label"] in {"REAL", "SIMULATED", "EMULATED", "DESIGN-ONLY"} for c in crit)


@pytest.mark.parametrize("name", ["MISSION.md", "CLAUDE.md", "PROGRESS.md", "DECISIONS.md", "ACCEPTANCE.md", "NEEDS_HUMAN.md", "STEER.md", "THIRD_PARTY.md", "run.sh"])
def test_continuity_file_exists(name):
    assert (ROOT / name).is_file()


@pytest.mark.parametrize("name", ["evaluator", "reviewer"])
def test_subagents_exist_and_evaluator_cannot_write(name):
    text = (ROOT / ".claude" / "agents" / f"{name}.md").read_text(encoding="utf-8")
    head = text.split("---")[1]
    tools = next(line for line in head.splitlines() if line.startswith("tools:"))
    assert "Write" not in tools and "Edit" not in tools


def test_hooks_cover_stop_steer_and_kill_switch():
    s = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    hooks = s["hooks"]
    assert "SessionStart" in hooks and "Stop" in hooks
    blob = json.dumps(hooks)
    assert "device_hook" in blob


def test_unattended_loop_spends_no_advisor_credits_by_default():
    """Reviewer R-1 M8: under `claude -p` Fable is billed without a consent prompt, so the loop's advisor is opt-in (MISSION 4.2)."""
    import os
    import subprocess
    text = (ROOT / "run.sh").read_text(encoding="utf-8")
    line = next(l for l in text.splitlines() if "ADVISOR_ARGS=(--advisor" in l)
    assert '${MAS_ADVISOR:-off}' in line and "fable" not in line, line
    out = subprocess.run(["bash", "-c", line + '; echo "${#ADVISOR_ARGS[@]}"'], capture_output=True, text=True,
                         env={k: v for k, v in os.environ.items() if k != "MAS_ADVISOR"})
    assert out.stdout.strip().splitlines()[-1] == "0", out



# ---- reviewer R-1 (DECISIONS.md): the oracle's own holes ---------------------------------------------

import os
import subprocess
import time


def test_m1_a_criterion_that_covers_its_own_table_stays_pass(world, monkeypatch):
    (world / "ACCEPTANCE.md").write_text(f"# A\n\n{acc.BEGIN}\nold table\n{acc.END}\n", encoding="utf-8")
    c = _crit([_test(world, "test_ok.py", "def test_a():\n    assert True\n")], covers=("src", "ACCEPTANCE.md"))
    r = acc.run_criterion(c, world)
    assert r["status"] == "PASS"
    acc.write_table([c], {c["id"]: r}, world / "ACCEPTANCE.md")  # rewriting the generated table is not a change to what was judged
    assert acc.effective([c], {"results": {c["id"]: r}}, world)[c["id"]]["status"] == "PASS"
    (world / "ACCEPTANCE.md").write_text((world / "ACCEPTANCE.md").read_text(encoding="utf-8") + "new prose\n", encoding="utf-8")
    assert acc.effective([c], {"results": {c["id"]: r}}, world)[c["id"]]["status"] == "STALE"


def test_m1_status_and_verify_write_nothing(world, monkeypatch):
    (world / "ACCEPTANCE.md").write_text(f"# A\n{acc.BEGIN}\n{acc.END}\n", encoding="utf-8")
    (world / "criteria.yaml").write_text("criteria:\n  - {id: X-1, phase: 1, title: t, label: REAL, tests: [t.py], covers: [src]}\n")
    monkeypatch.setattr(acc, "CRITERIA", world / "criteria.yaml")
    monkeypatch.setattr(acc, "RESULTS", world / "results.json")
    monkeypatch.setattr(acc, "ACCEPTANCE", world / "ACCEPTANCE.md")
    before = {q.name: q.read_bytes() for q in world.iterdir() if q.is_file()}
    acc.main(["status"])
    acc.main(["verify"])
    assert {q.name: q.read_bytes() for q in world.iterdir() if q.is_file()} == before


def test_m3_a_conftest_on_the_test_path_is_refused(world):
    (world / "conftest.py").write_text("def pytest_collection_modifyitems(config, items):\n    items[:] = [i for i in items if 'bad' not in i.name]\n")
    name = _test(world, "test_mix.py", "def test_ok():\n    assert True\n\ndef test_bad():\n    assert False\n")
    r = acc.run_criterion(_crit([name]), world)
    assert r["status"] == "FAIL" and "conftest" in r["reason"]


def test_m3_pytest_environment_cannot_deselect_or_inject(world, monkeypatch):
    monkeypatch.setenv("PYTEST_ADDOPTS", "-k ok")
    monkeypatch.setenv("PYTEST_PLUGINS", "nonexistent_plugin_xyz")
    name = _test(world, "test_mix.py", "def test_ok():\n    assert True\n\ndef test_bad():\n    assert False\n")
    r = acc.run_criterion(_crit([name]), world)
    assert r["status"] == "FAIL" and "1 failed" in r["reason"], r


def test_m3_repo_addopts_are_ignored(world):
    (world / "pyproject.toml").write_text('[tool.pytest.ini_options]\naddopts = "-k ok"\n')
    name = _test(world, "test_mix.py", "def test_ok():\n    assert True\n\ndef test_bad():\n    assert False\n")
    r = acc.run_criterion(_crit([name]), world)
    assert r["status"] == "FAIL" and "1 failed" in r["reason"], r


def test_m3_xfail_is_fail(world):
    body = "import pytest\ndef test_a():\n    pass\n@pytest.mark.xfail(strict=False)\ndef test_b():\n    assert False\n"
    assert acc.run_criterion(_crit([_test(world, "test_x.py", body)]), world)["status"] == "FAIL"


def test_m3_min_tests_is_enforced(world):
    c = _crit([_test(world, "test_ok.py", "def test_a():\n    assert True\n")]) | {"min_tests": 5}
    r = acc.run_criterion(c, world)
    assert r["status"] == "FAIL" and "min_tests" in r["reason"]


def test_m3_a_hang_is_fail(world):
    c = _crit([_test(world, "test_hang.py", "import time\ndef test_a():\n    time.sleep(30)\n")]) | {"timeout_s": 3}
    t0 = time.time()
    r = acc.run_criterion(c, world)
    assert r["status"] == "FAIL" and "timeout" in r["reason"] and time.time() - t0 < 25


def test_m3_the_fingerprint_covers_the_oracle_and_the_criterion_itself(world):
    c = _crit([_test(world, "test_ok.py", "def test_a():\n    assert True\n"), _test(world, "test_two.py", "def test_b():\n    assert True\n")])
    narrowed = dict(c, tests=c["tests"][:1])
    assert acc.fingerprint(c, world) != acc.fingerprint(narrowed, world)
    assert acc.fingerprint(c, world) != acc.fingerprint(dict(c, min_tests=1), world)
    text = Path(acc.__file__).read_bytes()
    assert hashlib.sha256(text.replace(b"\r\n", b"\n")).hexdigest() in acc.oracle_identity()


def test_m4_a_saboteur_the_tests_catch_keeps_the_pass(world):
    name = _test(world, "test_code.py", "def test_x():\n    assert 'X = 1' in open('src/unit_code.py').read()\n")
    c = _crit([name]) | {"saboteurs": [{"name": "x-two", "file": "src/unit_code.py", "old": "X = 1", "new": "X = 2"}]}
    r = acc.run_criterion(c, world)
    assert r["status"] == "PASS", r
    assert r["saboteurs"] == {"x-two": "killed"}


def test_m4_a_saboteur_that_survives_is_fail(world):
    (world / "src" / "unit_code.py").write_text("X = 1\n# note\n")
    name = _test(world, "test_code.py", "def test_x():\n    assert 'X = 1' in open('src/unit_code.py').read()\n")
    c = _crit([name]) | {"saboteurs": [{"name": "harmless", "file": "src/unit_code.py", "old": "# note", "new": "# other"}]}
    r = acc.run_criterion(c, world)
    assert r["status"] == "FAIL" and "survived" in r["reason"]


def test_m4_a_saboteur_whose_anchor_is_gone_is_fail(world):
    name = _test(world, "test_ok.py", "def test_a():\n    assert True\n")
    c = _crit([name]) | {"saboteurs": [{"name": "stale", "file": "src/unit_code.py", "old": "NOT THERE", "new": "x"}]}
    r = acc.run_criterion(c, world)
    assert r["status"] == "FAIL" and "anchor" in r["reason"]


def _git(world, *args):
    return subprocess.run(["git", *args], cwd=world, capture_output=True, text=True, check=True).stdout


def _repo(world, test_body):
    (world / "device" / "acceptance").mkdir(parents=True)
    (world / "t_one.py").write_text(test_body)
    (world / "device" / "acceptance" / "criteria.yaml").write_text(
        "criteria:\n  - {id: X-1, phase: 1, title: t, label: REAL, tests: [t_one.py], covers: [src]}\n")
    _git(world, "init", "-q")
    _git(world, "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    _git(world, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "x")


def test_m2_confirm_reruns_everything_at_head_and_ignores_recorded_results(world):
    _repo(world, "def test_a():\n    assert False\n")
    forged = {"results": {"X-1": {"status": "PASS", "fingerprint": "whatever"}}}
    (world / "test-results.json").write_text(json.dumps(forged))
    _git(world, "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    _git(world, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "forge")
    out = acc.confirm(world)
    assert out["ok"] is False and out["results"]["X-1"]["status"] == "FAIL"
    assert out["head"] == _git(world, "rev-parse", "HEAD").strip()


def test_m2_confirm_passes_a_clean_green_head(world):
    _repo(world, "def test_a():\n    assert True\n")
    out = acc.confirm(world)
    assert out["ok"] is True and out["results"]["X-1"]["status"] == "PASS"


def test_m2_confirm_refuses_a_dirty_tree(world):
    _repo(world, "def test_a():\n    assert True\n")
    (world / "src" / "unit_code.py").write_text("X = 2\n")
    out = acc.confirm(world)
    assert out["ok"] is False and "dirty" in out["reason"]


def test_m2_the_loop_stops_only_on_confirm():
    text = (ROOT / "run.sh").read_text(encoding="utf-8")
    import re
    assert re.search(r"if py scripts/device_accept\.py confirm[^\n]*\n[^\n]*break", text), "the loop must break on confirm"
    assert not re.search(r"if py scripts/device_accept\.py (status|verify|run)", text), "nothing but confirm may end the loop"


def test_a_criterion_covering_the_whole_tree_is_not_made_stale_by_its_own_result_or_by_skipped_folders(world):
    """P0-SECRETS covers ".": recording any result rewrites test-results.json, and runs/ and .venv change all the time. Neither is
    content under judgement; a real file anywhere else still is."""
    c = _crit(["tests/test_ok.py"], covers=(".",))
    before = acc.fingerprint(c, world)
    (world / "test-results.json").write_text('{"results": {"X": "anything"}}', encoding="utf-8")
    for skipped in ("runs", ".venv", "__pycache__"):
        (world / skipped / "deep").mkdir(parents=True, exist_ok=True)
        (world / skipped / "deep" / "f.txt").write_text("noise", encoding="utf-8")
    assert acc.fingerprint(c, world) == before
    (world / "docs").mkdir(exist_ok=True)
    (world / "docs" / "new.md").write_text("a real change", encoding="utf-8")
    assert acc.fingerprint(c, world) != before
    nested = world / "sub" / "test-results.json"  # only the oracle's own file at the root is excluded
    nested.parent.mkdir(exist_ok=True)
    after = acc.fingerprint(c, world)
    nested.write_text("{}", encoding="utf-8")
    assert acc.fingerprint(c, world) != after

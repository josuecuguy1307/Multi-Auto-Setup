"""The oracle itself is under test: it must be nearly perfect, because every later unit is optimised against it."""

from __future__ import annotations

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
    (tmp_path / "src" / "code.py").write_text("X = 1\n")
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
    (world / "src" / "code.py").write_text("X = 2\n")
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

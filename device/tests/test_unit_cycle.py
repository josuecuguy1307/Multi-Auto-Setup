"""P1-UNIT, the cycle itself: the history shows the unit's tests committed red (no implementation) before the green commit, and a fresh-context
evaluator report with VERDICT: PASS is on file."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEST, IMPL = "device/tests/test_approval.py", "src/mas_device/approval.py"


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def has(commit: str, path: str) -> bool:
    return subprocess.run(["git", "cat-file", "-e", f"{commit}:{path}"], cwd=ROOT, capture_output=True).returncode == 0


def test_red_commit_precedes_green_commit():
    commits = git("rev-list", "--reverse", "HEAD").split()
    red = [c for c in commits if has(c, TEST) and not has(c, IMPL)]
    green = [c for c in commits if has(c, TEST) and has(c, IMPL)]
    assert red, "no commit has the tests without the implementation"
    assert green, "no commit has both"
    assert commits.index(red[0]) < commits.index(green[0])


def test_evaluator_report_passes():
    report = (ROOT / "device" / "evals" / "evaluator" / "P1-UNIT.md").read_text(encoding="utf-8")
    assert "UNIT: P1-UNIT" in report
    verdicts = [l.split(":", 1)[1].strip() for l in report.splitlines() if l.startswith("VERDICT:")]
    assert verdicts and verdicts[-1] == "PASS"

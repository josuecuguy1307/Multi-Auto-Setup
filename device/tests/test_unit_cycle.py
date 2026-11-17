"""P1-UNIT, the cycle itself (reviewer R-1 M5): the history proves the unit's tests were committed red before the implementation, by RUNNING
them at those commits (not only by looking at which files existed), and a fresh-context evaluator report with VERDICT: PASS is on file,
bound to the exact code it judged (a hash of the implementation files)."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEST, IMPL = "device/tests/test_approval.py", "src/mas_device/approval.py"
JUDGED = ("src/mas_device/approval.py", "src/mas_device/keymodel.py")
INI = ROOT / "device" / "acceptance" / "pytest.ini"


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=True).stdout


def has(commit: str, path: str) -> bool:
    return subprocess.run(["git", "cat-file", "-e", f"{commit}:{path}"], cwd=ROOT, capture_output=True).returncode == 0


def first_commits() -> tuple[str, str]:
    commits = git("rev-list", "--reverse", "HEAD").split()
    red = [c for c in commits if has(c, TEST) and not has(c, IMPL)]
    green = [c for c in commits if has(c, TEST) and has(c, IMPL)]
    assert red, "no commit has the tests without the implementation"
    assert green, "no commit has both"
    return red[0], green[0]


def run_tests_at(commit: str) -> subprocess.CompletedProcess:
    tmp = Path(tempfile.mkdtemp(prefix="cycle-"))
    wt = tmp / "wt"
    try:
        subprocess.run(["git", "worktree", "add", "--detach", str(wt), commit], cwd=ROOT, capture_output=True, check=True)
        env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_") and k != "PYTHONPATH"}
        env.update(PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(wt / "src"))  # that commit's code, not the installed copy
        return subprocess.run([sys.executable, "-m", "pytest", "-c", str(INI), f"--rootdir={wt}", "-q", "-p", "no:cacheprovider", TEST],
                              cwd=wt, capture_output=True, text=True, env=env, timeout=600)
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=ROOT, capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)


def test_red_commit_precedes_green_commit():
    red, green = first_commits()
    commits = git("rev-list", "--reverse", "HEAD").split()
    assert commits.index(red) < commits.index(green)


def test_the_red_tests_are_real_tests_and_really_fail_at_the_red_commit():
    red, _ = first_commits()
    text = git("show", f"{red}:{TEST}")
    assert len(re.findall(r"^def test_", text, re.M)) >= 10, "an empty or stub test file is not a red test"
    r = run_tests_at(red)
    assert r.returncode != 0, "the tests did not fail before the implementation existed"


def test_the_tests_pass_at_the_green_commit():
    _, green = first_commits()
    r = run_tests_at(green)
    assert r.returncode == 0, r.stdout[-2000:]


def judged_hash() -> str:
    h = hashlib.sha256()
    for rel in JUDGED:
        h.update(rel.encode() + b"\0" + hashlib.sha256((ROOT / rel).read_bytes().replace(b"\r\n", b"\n")).hexdigest().encode() + b"\n")
    return h.hexdigest()


def test_evaluator_report_passes_and_judged_this_code():
    report = (ROOT / "device" / "evals" / "evaluator" / "P1-UNIT.md").read_text(encoding="utf-8")
    assert "UNIT: P1-UNIT" in report
    verdicts = [l.split(":", 1)[1].strip() for l in report.splitlines() if l.startswith("VERDICT:")]
    assert verdicts and verdicts[-1] == "PASS"
    m = re.search(r"^Code-Hash: ([0-9a-f]{64})$", report, re.M)
    assert m, "the report must carry the Code-Hash of the code it judged"
    assert m.group(1) == judged_hash(), "the code changed since the evaluator judged it: run the evaluator again"

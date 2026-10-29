"""The recorded runs the V2 and M2 checks read (`runs/`, git-ignored, kept only on the machine that made them).

A clean checkout has none. The two checks then say "not measured" (a skip with that reason), never "passed". Where recorded runs exist the guard is a no-op: the checks run exactly as before,
and a skip is not possible (conformance/test_recorded_runs.py asserts both branches).
"""
from __future__ import annotations

import glob
from pathlib import Path

import pytest

NOT_MEASURED = "not measured: no recorded runs in this checkout"


def recorded_run_dbs(root: Path) -> list[str]:
    """The event databases of the recorded runs under `root/runs` (both layouts)."""
    runs = Path(root) / "runs"
    return sorted(glob.glob(str(runs / "*" / "*" / "envrec.db")) + glob.glob(str(runs / "*" / "envrec.db")))


def require_recorded_runs(root: Path) -> list[str]:
    """The databases, or a skip with the reason when there are none. Never skips when there is at least one."""
    dbs = recorded_run_dbs(root)
    if not dbs:
        pytest.skip(NOT_MEASURED)
    return dbs

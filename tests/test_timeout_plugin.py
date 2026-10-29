"""A4: a test that hangs becomes a failure named HANG. These tests run a tiny pytest session in a subprocess so the hang is real."""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def run_session(tmp_path, body: str, *args: str, env_timeout: str | None = None, wall: float = 60):
    (tmp_path / "test_x.py").write_text(textwrap.dedent(body))
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT / "testplugins")}
    if env_timeout:
        env["ENVREC_TEST_TIMEOUT"] = env_timeout
    return subprocess.run([sys.executable, "-m", "pytest", "-p", "envrec_timeout", "-p", "no:cacheprovider", "-q", str(tmp_path / "test_x.py"), *args],
                          capture_output=True, text=True, cwd=tmp_path, env=env, timeout=wall)


def test_a_sleeping_test_fails_with_hang_after_its_limit_and_the_next_test_still_runs(tmp_path):
    done = run_session(tmp_path, """
        import time
        def test_hangs():
            time.sleep(30)
        def test_after():
            assert 1 + 1 == 2
        """, env_timeout="1")
    assert done.returncode == 1 and "1 failed, 1 passed" in done.stdout and "HANG: " in done.stdout and "ran longer than 1 s" in done.stdout


def test_a_test_blocked_reading_a_pipe_that_never_writes_also_becomes_a_failure(tmp_path):
    done = run_session(tmp_path, """
        import os
        def test_blocks_on_read():
            r, w = os.pipe()
            os.read(r, 1)
        """, env_timeout="1")
    assert done.returncode == 1 and "HANG: " in done.stdout and "1 failed" in done.stdout


def test_a_marker_gives_a_slow_test_more_time_and_zero_turns_the_limit_off(tmp_path):
    done = run_session(tmp_path, """
        import time, pytest
        @pytest.mark.timeout(10)
        def test_slow_but_allowed():
            time.sleep(2)
        @pytest.mark.timeout(0)
        def test_unlimited():
            time.sleep(1.5)
        """, env_timeout="1")
    assert done.returncode == 0 and "2 passed" in done.stdout


def test_a_fast_test_is_not_touched(tmp_path):
    done = run_session(tmp_path, """
        def test_fast():
            assert sum(range(10)) == 45
        """)
    assert done.returncode == 0 and "1 passed" in done.stdout

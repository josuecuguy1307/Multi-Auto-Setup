"""A hard time limit per test, with no dependency (pytest-timeout is not installed here). A test that hangs becomes a FAILURE named HANG instead of a suite that never ends.

The limit is 120 s unless ENVREC_TEST_TIMEOUT says otherwise, and a test that legitimately needs longer says so with `@pytest.mark.timeout(seconds)`. It works with SIGALRM: the
alarm interrupts a blocking read, a sleep or a lock wait in the main thread and raises there. If the process cannot be interrupted (stuck inside a C call), a `faulthandler`
watchdog 30 s later prints every thread's stack and ends the process, which a caller sees as a non-zero exit with no summary: also a failure, never a hang."""

from __future__ import annotations

import faulthandler
import os
import signal
import threading

import pytest

DEFAULT_SECONDS = float(os.environ.get("ENVREC_TEST_TIMEOUT", "120"))
WATCHDOG_EXTRA_S = 30


class HangTimeout(Exception):
    """A test ran longer than its limit."""


def pytest_configure(config):
    config.addinivalue_line("markers", "timeout(seconds): this test may run longer than the default limit (0 turns the limit off)")


def _limit(item) -> float:
    m = item.get_closest_marker("timeout")
    return float(m.args[0]) if m is not None and m.args else DEFAULT_SECONDS


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_call(item):
    limit = _limit(item)
    if limit <= 0 or not hasattr(signal, "SIGALRM") or threading.current_thread() is not threading.main_thread():
        yield
        return

    def on_alarm(signum, frame):
        raise HangTimeout(f"HANG: {item.nodeid} ran longer than {limit:g} s")

    previous = signal.signal(signal.SIGALRM, on_alarm)
    signal.setitimer(signal.ITIMER_REAL, limit)
    faulthandler.dump_traceback_later(limit + WATCHDOG_EXTRA_S, exit=True)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        faulthandler.cancel_dump_traceback_later()
        signal.signal(signal.SIGALRM, previous)

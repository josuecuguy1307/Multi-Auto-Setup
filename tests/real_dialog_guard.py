"""No test may ever open a real operating-system dialog (Touch ID, a hidden-field window, a code notice). The three functions that would are replaced, for EVERY test, by wrappers that open nothing: when
called the way production calls them (no runner of the test's own handed in) they record the call, answer "unavailable" and the test FAILS at its end with the name of the call. A test that exercises the
real functions hands them its own fake runner (`run=`, `popen=`) and passes through untouched. Registered for the whole suite in conftest.py (`pytest_plugins`), so a new test cannot forget it."""
from __future__ import annotations

import pytest

import envrec_os.confirm as osconfirm
import envrec_os.secret_prompt as ossecret

# (module, function, the argument a test passes to put its own fake in place of the operating system, what the function answers when nothing is shown)
GUARDED = (
    (osconfirm, "native_confirm", "run", {"result": "unavailable", "mechanism": None, "strong": None, "assurance": None, "reason": "no real dialog in a test"}),
    (osconfirm, "native_notice", "popen", None),
    (ossecret, "native_secret", "run", {"result": "unavailable", "mechanism": None, "value": None}),
)


def _wrap(orig, name: str, injected: str, answer, seen: list):
    def guarded(*args, **kwargs):
        if injected in kwargs:  # the test's own fake stands for the operating system: nothing real can start
            return orig(*args, **kwargs)
        shown = args[1] if len(args) > 1 else kwargs.get("message", "")
        seen.append(f"{name}({str(args[0] if args else kwargs.get('title', ''))[:60]!r}, {str(shown)[:70]!r})")
        return dict(answer) if isinstance(answer, dict) else answer
    guarded.__wrapped__ = orig
    guarded.is_real_dialog_guard = True
    return guarded


@pytest.fixture(autouse=True)
def no_real_os_dialog(monkeypatch):
    seen: list[str] = []
    for module, name, injected, answer in GUARDED:
        real = getattr(module, name)
        real = getattr(real, "__wrapped__", real)
        monkeypatch.setattr(module, name, _wrap(real, name, injected, answer, seen))
    yield seen
    assert not seen, "a REAL operating-system dialog was requested by this test (it must use a fake dialog): " + "; ".join(seen)

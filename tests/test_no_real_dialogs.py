"""The guard that keeps every test from opening a real operating-system dialog. A real Touch ID prompt once appeared on the owner's screen from a test run: the harness opens approval dialogs by itself now,
and a test that starts a chat-led flow with no fake dialog of its own reached the real one. SIMULATED-LOCAL."""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import envrec_os.confirm as osconfirm
import envrec_os.secret_prompt as ossecret
import real_dialog_guard as G

TESTS = Path(__file__).resolve().parent


def test_the_three_real_dialog_functions_are_guards_in_every_test():
    for module, name, _inj, _ans in G.GUARDED:
        assert getattr(getattr(module, name), "is_real_dialog_guard", False), f"{module.__name__}.{name} can reach the real dialog"


def test_a_call_the_way_production_makes_it_opens_nothing_and_is_recorded(no_real_os_dialog):
    assert osconfirm.native_confirm("Multi Auto Setup", "Run step 2 of 6")["result"] == "unavailable"
    assert ossecret.native_secret("Store a key", "MODEL_API_KEY")["result"] == "unavailable"
    assert osconfirm.native_notice("Pairing code", "123456") is None
    assert len(no_real_os_dialog) == 3 and "native_confirm" in no_real_os_dialog[0]
    no_real_os_dialog.clear()  # this test provoked it on purpose; any other test that does so fails (next test)


def test_a_test_that_hands_the_functions_its_own_fake_passes_through_untouched(no_real_os_dialog):
    calls = []

    def fake_run(argv, env, timeout):
        calls.append(argv[0])
        return 0, "ok"
    osconfirm.native_confirm("t", "m", fam="darwin", run=fake_run, which=lambda n: "/usr/bin/" + n)
    assert calls and no_real_os_dialog == []


def test_a_test_that_reaches_the_real_dialog_fails(tmp_path):
    """Run a throwaway test file in its own pytest with the same guard: it calls the dialog the way the harness does and must FAIL, naming the call."""
    (tmp_path / "test_leaky.py").write_text(textwrap.dedent('''
        import envrec_os.confirm as osconfirm

        def test_leaks():
            osconfirm.native_confirm("Multi Auto Setup", "Run step 2 of 6 on node1")
    '''))
    out = subprocess.run([sys.executable, "-m", "pytest", "-p", "real_dialog_guard", "-p", "no:cacheprovider", "-q", str(tmp_path / "test_leaky.py"), "--rootdir", str(tmp_path), "-c", "/dev/null"],
                         capture_output=True, text=True, timeout=120, env={**__import__("os").environ, "PYTHONPATH": f"{TESTS}:{TESTS.parent / 'src'}"})
    assert out.returncode != 0 and "REAL operating-system dialog" in out.stdout + out.stderr and "native_confirm" in out.stdout

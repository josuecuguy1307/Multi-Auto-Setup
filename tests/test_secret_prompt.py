"""The hidden-field prompt layer: data per OS, program output read, value returned only to the caller, never invented. SIMULATED-LOCAL: a fake runner stands for osascript, PowerShell, zenity and kdialog."""
from __future__ import annotations

import pytest

from envrec_os import secret_prompt as S

SENTINEL = "LLM_0_SENTINEL_typed"


def run_returning(code, out):
    seen = {}

    def run(argv, env, timeout):
        seen.update(argv=argv, env=env)
        return code, out
    return run, seen


def test_macos_reads_the_prefixed_value_and_passes_title_and_message_as_arguments_not_a_shell_string():
    run, seen = run_returning(0, f"value:{SENTINEL}\n")
    out = S.native_secret("Store a key", "Store MODEL_API_KEY on the Lenovo?", fam="darwin", run=run, which=lambda n: "/usr/bin/" + n)
    assert out == {"result": "entered", "mechanism": "macos_hidden_dialog", "value": SENTINEL}
    assert seen["argv"][0] == "/usr/bin/osascript" and "Store a key" in seen["argv"] and "Store MODEL_API_KEY on the Lenovo?" in seen["argv"] and "with hidden answer" in seen["argv"][2]


def test_linux_uses_exit_codes_and_an_empty_entry_is_not_a_value():
    run, _ = run_returning(0, f"{SENTINEL}\n")
    assert S.native_secret("t", "m", fam="linux", run=run, which=lambda n: "/usr/bin/" + n)["value"] == SENTINEL
    for code, want in ((1, "declined"), (5, "timeout")):
        run, _ = run_returning(code, "")
        assert S.native_secret("t", "m", fam="linux", run=run, which=lambda n: "/usr/bin/" + n)["result"] == want
    run, _ = run_returning(0, "\n")
    out = S.native_secret("t", "m", fam="linux", run=run, which=lambda n: "/usr/bin/" + n)
    assert out["result"] == "declined" and out["value"] is None


def test_windows_environment_carries_the_text_not_the_command_line_and_cancel_is_declined():
    run, seen = run_returning(0, "declined\n")
    out = S.native_secret("T", "M", fam="windows", run=run, which=lambda n: "C:/x/" + n)
    assert out["result"] == "declined" and seen["env"]["MAS_SECRET_TITLE"] == "T" and seen["env"]["MAS_SECRET_MESSAGE"] == "M"


def test_no_program_is_unavailable_and_a_timeout_is_a_timeout_and_nothing_is_invented():
    assert S.native_secret("t", "m", fam="darwin", run=lambda *a: (_ for _ in ()).throw(AssertionError("nothing may start")), which=lambda n: None) == {"result": "unavailable", "mechanism": None, "value": None}
    run, _ = run_returning(None, "timeout")
    assert S.native_secret("t", "m", fam="darwin", run=run, which=lambda n: "/x")["result"] == "timeout"
    run, _ = run_returning(None, "unavailable")
    assert S.native_secret("t", "m", fam="darwin", run=run, which=lambda n: "/x")["result"] == "unavailable"


def test_a_value_longer_than_the_limit_is_refused_not_cut():
    run, _ = run_returning(0, "value:" + "x" * (S.MAX_VALUE + 1))
    out = S.native_secret("t", "m", fam="darwin", run=run, which=lambda n: "/x")
    assert out["result"] == "declined" and out["value"] is None


def test_every_family_has_a_prompt_and_the_table_names_no_value():
    t = S.table()
    assert all(t[f]["tools"] for f in ("darwin", "windows", "linux")) and SENTINEL not in str(t)


@pytest.mark.parametrize("fam", ["darwin", "linux", "windows"])
def test_on_each_family_the_window_is_found_by_the_name_the_os_layer_gives_the_family(fam, monkeypatch):
    """The first real use on a Mac found no window: the table was keyed `macos` while the layer says `darwin`, so the key entry ended `unavailable` in 3 ms. Tests passed because they named the family
    themselves. Here the family comes from the layer's own function, as in a real run."""
    monkeypatch.setattr(S, "family", lambda: fam)
    assert S.available(which=lambda n: "/x/" + n)["available"] is True
    assert set(S.table()) - {"note"} == {"darwin", "linux", "windows"}  # a name the layer never returns can never be found

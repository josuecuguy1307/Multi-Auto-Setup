"""The operating system's native confirmation (envrec_os/confirm.py, data in skills/os-confirm). The programs are fakes: what is checked is what is asked, in what order, and what the answer counts as."""
from __future__ import annotations

import json

import pytest

from envrec_os import confirm as C

PLAN = "Instalar llama.cpp\nComando: curl X\nDeshacer: rm X\nplan sha256:" + "ab" * 32


class Fake:
    """Plays the dialog program and the biometric helper; records every call."""

    def __init__(self, dialog="approved", strong_probe="available", strong_ask="approved"):
        self.dialog, self.strong_probe, self.strong_ask, self.calls = dialog, strong_probe, strong_ask, []

    def which(self, exe):
        return f"/bin/{exe}"

    def run(self, argv, env, timeout):
        self.calls.append((argv, env))
        exe = argv[0].rsplit("/", 1)[-1]
        if exe == "swift":
            return (0, self.strong_probe if "--probe" in argv else self.strong_ask)
        if exe == "powershell" and env.get("MAS_STRONG_MODE"):
            return (0, self.strong_probe if env["MAS_STRONG_MODE"] == "probe" else self.strong_ask)
        return (0, self.dialog)

    def shown(self):
        """The text the owner was shown by the dialog program (the first non-probe call)."""
        argv, env = self.calls[1] if len(self.calls) > 1 and "--probe" in self.calls[0][0] else self.calls[0]
        return " ".join(argv) + " " + " ".join(env.get(k, "") for k in ("MAS_CONFIRM_MESSAGE",))


def go(fam, fake, **kw):
    return C.native_confirm("Multi Auto Setup", PLAN, fam=fam, run=fake.run, which=fake.which, **kw)


def test_where_the_machine_can_verify_its_owner_the_button_alone_approves_nothing():
    f = Fake(dialog="approved", strong_ask="declined")
    r = go("darwin", f, reason="Multi Auto Setup: plan sha256:abababab…")
    assert r["result"] == "declined" and r["assurance"] is None  # the button was pressed (by anyone, or by an agent that controls the screen): without Touch ID or the password it counts for nothing
    asks = [c for c in f.calls if c[0][-1] not in ("--probe",) and c[0][0].endswith("swift")]
    assert len(asks) == 1 and "plan sha256:" in asks[0][0][-1]  # the system prompt names the plan too


def test_with_the_proof_the_answer_is_strong_and_says_which():
    r = go("darwin", Fake())
    assert r == {"result": "approved", "mechanism": "macos_dialog", "strong": "touch_id", "assurance": "strong"}


def test_the_dialog_says_the_proof_is_coming_and_the_plan_is_in_it():
    f = Fake()
    go("darwin", f)
    dialog = next(c for c in f.calls if c[0][0].endswith("osascript"))
    text = " ".join(dialog[0])
    assert "Touch ID" in text and "sha256:" + "ab" * 32 in text and "Instalar llama.cpp" in text and "curl X" in text and "rm X" in text


def test_where_it_cannot_the_button_is_accepted_but_labeled_weaker_in_the_dialog_and_in_the_result():
    f = Fake(strong_probe="unavailable")
    r = go("darwin", f)
    assert r == {"result": "approved", "mechanism": "macos_dialog", "strong": None, "assurance": "weak"}
    dialog = next(c for c in f.calls if c[0][0].endswith("osascript"))
    assert "WEAKER" in " ".join(dialog[0]) and not [c for c in f.calls if c[0][0].endswith("swift") and "--probe" not in c[0]]  # no biometric was asked for: there is none


def test_a_dialog_that_was_declined_or_not_answered_never_reaches_the_proof_step():
    for dialog, want in (("declined", "declined"), ("timeout", "timeout")):
        f = Fake(dialog=dialog)
        assert go("darwin", f)["result"] == want
        assert not [c for c in f.calls if c[0][0].endswith("swift") and "--probe" not in c[0]]


def test_windows_asks_for_windows_hello_and_labels_the_weaker_path():
    f = Fake(dialog="approved")
    r = go("windows", f)
    assert r["result"] == "approved" and r["strong"] == "windows_hello" and r["assurance"] == "strong"
    modes = [c[1].get("MAS_STRONG_MODE") for c in f.calls if c[1].get("MAS_STRONG_MODE")]
    assert modes == ["probe", "ask"]
    assert go("windows", Fake(strong_probe="unavailable"))["assurance"] == "weak"
    g = Fake()
    go("windows", g, reason="Multi Auto Setup: plan sha256:abababab…")
    ask = next(c for c in g.calls if c[1].get("MAS_STRONG_MODE") == "ask")
    assert "plan sha256:" in ask[1]["MAS_CONFIRM_REASON"]  # the reason the system prompt shows names the plan


def test_linux_has_no_biometric_in_the_table_so_it_is_always_the_weaker_path():
    r = go("linux", Fake())
    assert r["assurance"] == "weak" and r["strong"] is None


def test_nothing_available_is_unavailable_never_a_guess():
    f = Fake()
    f.which = lambda exe: None
    assert go("darwin", f) == {"result": "unavailable", "mechanism": None, "strong": None, "assurance": None}


def test_the_text_travels_as_separate_arguments_never_through_a_shell():
    f = Fake()
    hostile = '"; do shell script "rm -rf ~" ; "'
    C.native_confirm("T", hostile, fam="darwin", run=f.run, which=f.which)
    argv = next(c[0] for c in f.calls if c[0][0].endswith("osascript"))
    assert any(hostile in a for a in argv) and not any(a in ("sh", "bash", "-c") for a in argv)


def test_the_table_is_data_the_layer_reads():
    d = json.loads(C.DIALOGS_FILE.read_text())
    assert d["darwin"]["tools"][0]["strong"]["id"] == "touch_id" and d["windows"]["tools"][0]["strong"]["id"] == "windows_hello" and "WEAKER" in d["weak_note"]

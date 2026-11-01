"""`mas service install|status|uninstall`: what is asked, what is refused, what is written. The OS commands are a fake runner; the files are real, in a temporary profile. SIMULATED-LOCAL."""
from __future__ import annotations

import argparse
import sys

import pytest

from envrec_mas import lifecycle
from envrec_node.state import NodeState

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the registry-based Windows row has no file to inspect; covered in test_service_layer.py")


class Fake:
    def __init__(self):
        self.calls = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        return 0, ""


def ns(tmp_path, action, role="node", **kw):
    return argparse.Namespace(state_dir=str(tmp_path / "state"), action=action, role=role, yes=kw.get("yes", True), bind=kw.get("bind"), port=kw.get("port"))


@pytest.fixture(autouse=True)
def profile(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "h" / "cfg"))
    monkeypatch.setenv("MAS_HOME", str(tmp_path / "mashome"))
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "test")
    (tmp_path / "h").mkdir()


def pair(tmp_path):
    st = NodeState(tmp_path / "state")
    st.save_pin("127.0.0.1:7443", st.identity().cert_der)


def files(tmp_path):
    return [p for p in (tmp_path / "h").rglob("*") if p.is_file()]


def test_an_unpaired_node_gets_no_service_and_nothing_is_run(tmp_path, capsys):
    f = Fake()
    assert lifecycle.cmd_service(ns(tmp_path, "install"), runner=f) == 1
    assert "mas join" in capsys.readouterr().err and files(tmp_path) == [] and f.calls == []


def test_a_coordinator_service_needs_the_address_and_says_it_changes_no_firewall(tmp_path, capsys):
    assert lifecycle.cmd_service(ns(tmp_path, "install", role="coordinator"), runner=Fake()) == 2
    assert files(tmp_path) == []
    f = Fake()
    assert lifecycle.cmd_service(ns(tmp_path, "install", role="coordinator", bind="192.0.2.7"), runner=f) == 0
    out = capsys.readouterr().out
    assert "never changes the firewall" in out and "--bind 192.0.2.7" in out
    unit = files(tmp_path)[0].read_text()
    assert "192.0.2.7" in unit and "coordinator" in unit and "start" in unit and "--bind" in unit


def test_the_owner_sees_the_file_and_the_undo_first_and_a_no_writes_nothing(tmp_path, capsys, monkeypatch):
    pair(tmp_path)
    monkeypatch.setattr("builtins.input", lambda p="": "n")
    f = Fake()
    assert lifecycle.cmd_service(ns(tmp_path, "install", yes=False), runner=f) == 1
    out = capsys.readouterr().out
    assert "How to undo it" in out and "File that is written" in out and "service-node.log" in out
    assert files(tmp_path) == [] and not any(c[:2] == ["launchctl", "bootstrap"] or c[:3] == ["systemctl", "--user", "enable"] for c in f.calls)


def test_install_status_uninstall_round_trip_and_the_update_note(tmp_path, capsys):
    pair(tmp_path)
    f = Fake()
    assert lifecycle.cmd_service(ns(tmp_path, "install"), runner=f) == 0
    written = files(tmp_path)
    assert len(written) == 1 and "node" in written[0].name and str(tmp_path / "state") in written[0].read_text()
    assert (tmp_path / "mashome" / "service-node.json").is_file()  # what `mas update` re-registers
    assert lifecycle.cmd_service(ns(tmp_path, "status"), runner=f) == 0
    assert "service: installed" in capsys.readouterr().out
    assert lifecycle.cmd_service(ns(tmp_path, "uninstall"), runner=f) == 0
    assert files(tmp_path) == [] and not (tmp_path / "mashome" / "service-node.json").exists()
    assert lifecycle.cmd_service(ns(tmp_path, "uninstall"), runner=f) == 0  # again: nothing to do, no error
    assert lifecycle.cmd_service(ns(tmp_path, "status"), runner=f) == 1

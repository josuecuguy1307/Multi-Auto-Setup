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
    from envrec_mas import coordconfig
    given = {k: kw.get(k) for k in coordconfig.load_schema()["keys"]}  # the settings a flag gave (None: not given)
    return argparse.Namespace(state_dir=str(tmp_path / "state"), action=action, role=role, yes=kw.get("yes", True), **given)


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
    assert "never changes the firewall" in out and '"bind": "192.0.2.7"' in out  # the settings are shown before anything is written
    unit = files(tmp_path)[0].read_text()
    assert "coordinator" in unit and "start" in unit and "--bind" not in unit and "192.0.2.7" not in unit  # how it runs is data, not part of the command
    import json
    assert json.loads((tmp_path / "state" / "coordinator.json").read_text()) == {"bind": "192.0.2.7", "web": True}  # a new file gets the schema's service defaults


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


# ---- the coordinator service: settings as data ----------------------------------------------------------------------------------------------------------------------

def test_the_coordinator_service_takes_every_setting_a_real_coordinator_needs_and_writes_them_as_data(tmp_path, capsys):
    import json
    f = Fake()
    assert lifecycle.cmd_service(ns(tmp_path, "install", role="coordinator", bind="192.0.2.7", port=7500, discovery_port=7501, name="Mac", model_profile="qwen-local",
                                    models_file="/x/models.yaml", web_port=8090), runner=f) == 0
    cfg = json.loads((tmp_path / "state" / "coordinator.json").read_text())
    assert cfg == {"bind": "192.0.2.7", "discovery_port": 7501, "model_profile": "qwen-local", "models_file": "/x/models.yaml", "name": "Mac", "port": 7500, "web": True, "web_port": 8090}
    unit = files(tmp_path)[0].read_text()
    assert "--state-dir" in unit and str(tmp_path / "state") in unit and all(x not in unit for x in ("7500", "qwen-local", "models.yaml", "--web"))


def test_installing_again_changes_only_what_was_given_and_no_web_turns_the_chat_bridge_off(tmp_path):
    import json
    lifecycle.cmd_service(ns(tmp_path, "install", role="coordinator", bind="192.0.2.7", model_profile="a"), runner=Fake())
    lifecycle.cmd_service(ns(tmp_path, "install", role="coordinator", model_profile="b"), runner=Fake())  # no bind given: the file's stays
    assert json.loads((tmp_path / "state" / "coordinator.json").read_text()) == {"bind": "192.0.2.7", "model_profile": "b", "web": True}
    lifecycle.cmd_service(ns(tmp_path, "install", role="coordinator", web=False), runner=Fake())
    assert json.loads((tmp_path / "state" / "coordinator.json").read_text())["web"] is False


def test_a_bad_setting_is_refused_before_anything_is_written_and_a_misspelt_key_in_the_file_is_refused_not_ignored(tmp_path, capsys):
    from envrec_mas import coordconfig
    f = Fake()
    assert lifecycle.cmd_service(ns(tmp_path, "install", role="coordinator", bind="192.0.2.7", port="7500"), runner=f) == 2  # a string where an int belongs
    assert "bad_type" in capsys.readouterr().err and files(tmp_path) == [] and f.calls == [] and not (tmp_path / "state" / "coordinator.json").exists()
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state" / "coordinator.json").write_text('{"bind": "192.0.2.7", "prot": 1}')
    with pytest.raises(coordconfig.ConfigRefused) as e:
        coordconfig.read(tmp_path / "state")
    assert e.value.reason == "unknown_key" and "prot" in e.value.text


def test_uninstall_removes_the_service_and_leaves_the_settings_and_says_so(tmp_path, capsys):
    lifecycle.cmd_service(ns(tmp_path, "install", role="coordinator", bind="192.0.2.7"), runner=Fake())
    capsys.readouterr()
    assert lifecycle.cmd_service(ns(tmp_path, "uninstall", role="coordinator"), runner=Fake()) == 0
    assert "coordinator.json" in capsys.readouterr().out and (tmp_path / "state" / "coordinator.json").is_file() and files(tmp_path) == []


def test_the_command_line_beats_the_file_and_the_file_beats_the_default(tmp_path):
    from envrec_mas import coordconfig
    st = tmp_path / "state"
    coordconfig.write(st, {"bind": "192.0.2.7", "port": 7500, "web": True})
    r = coordconfig.resolve({"port": 7600}, st)
    assert (r["bind"], r["port"], r["web"], r["discovery_port"], r["idle_timeout"]) == ("192.0.2.7", 7600, True, 7444, 30.0)
    assert coordconfig.resolve({"web": False}, st)["web"] is False  # an explicit False is a value, not 'not given'


def test_every_setting_in_the_data_has_a_flag_on_both_commands_and_none_is_hard_coded():
    import argparse
    from envrec_mas import coordconfig
    p = argparse.ArgumentParser()
    coordconfig.add_flags(p)
    flags = {a for act in p._actions for a in act.option_strings}
    schema = coordconfig.load_schema()
    assert all(spec["flag"] in flags for spec in schema["keys"].values()) and "--no-web" in flags
    ns_ = p.parse_args([])
    assert coordconfig.from_namespace(ns_) == {}  # nothing given: the parser invents no value that would beat the file
    assert coordconfig.from_namespace(p.parse_args(["--web", "--port", "9"])) == {"web": True, "port": 9}


def test_a_service_that_dies_comes_back_on_macos_and_the_windows_row_says_it_does_not():
    import json
    from envrec_os import SKILLS_DIR
    ref = SKILLS_DIR / "os-service" / "references"
    plist = (ref / "launchd.plist.tmpl").read_text()
    assert "<key>KeepAlive</key>" in plist and "<key>SuccessfulExit</key><false/>" in plist and "<key>ThrottleInterval</key>" in plist
    win = json.loads((ref / "service.json").read_text())["windows"][0]
    assert "does not restart by itself" in win["what"]  # said to the owner, not hidden


def test_the_default_models_file_is_this_programs_own_not_one_relative_to_the_working_directory():
    from envrec_coordinator.runs import DEFAULT_MODELS_FILE
    assert DEFAULT_MODELS_FILE.is_absolute() and DEFAULT_MODELS_FILE.name == "models.yaml" and DEFAULT_MODELS_FILE.parent.name == "config"


def test_coordinator_start_without_an_address_anywhere_is_refused_with_a_typed_reason(tmp_path, capsys):
    from envrec_mas import cli
    assert cli.main(["--state-dir", str(tmp_path / "s"), "coordinator", "start"]) == 2
    assert "no_bind" in capsys.readouterr().err


def test_a_coordinator_started_with_no_flags_runs_from_its_settings_file_listens_and_opens_the_chat_bridge(tmp_path):
    """SIMULATED-LOCAL, real process: the command a service runs (`coordinator start`, nothing else) reads <state>/coordinator.json."""
    import json
    import socket
    import subprocess
    import time
    from envrec_mas import coordconfig
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        port = sk.getsockname()[1]
    st = tmp_path / "s"
    coordconfig.write(st, {"bind": "127.0.0.1", "port": port, "no_discovery": True, "web": True})
    proc = subprocess.Popen([sys.executable, "-m", "envrec_mas", "--state-dir", str(st), "coordinator", "start"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        ep = st / "coordinator" / "harness" / "endpoint.json"
        end = time.time() + 30
        while time.time() < end and not ep.exists() and proc.poll() is None:
            time.sleep(0.2)
        assert ep.exists(), "the chat bridge endpoint was not written (web was in the settings file)"
        assert json.loads(ep.read_text())["host"] == "127.0.0.1"
        with socket.create_connection(("127.0.0.1", port), timeout=5):
            pass  # the one TCP port, on the address from the file
    finally:
        proc.terminate()
        out = proc.communicate(timeout=20)[0]
    assert f"listening on 127.0.0.1:{port}" in out

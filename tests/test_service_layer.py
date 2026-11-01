"""The per-user service layer (`envrec_os.service`) for ALL THREE families, driven by a fake command runner. SIMULATED-LOCAL: nothing real is started; what is asserted is what would be
written and run. The real darwin run is in test_lifecycle_install.py's notes; Windows is UNTESTED here (static only)."""
from __future__ import annotations

import plistlib

import pytest

from envrec_os import service as svc


class Fake:
    def __init__(self, fail=(), absent=()):
        self.calls, self.fail, self.absent = [], set(fail), set(absent)

    def __call__(self, argv):
        self.calls.append(list(argv))
        key = argv[0]
        if key in self.absent:
            return 127, "not found"
        return (1, "boom") if any(f in " ".join(argv) for f in self.fail) else (0, "")


ARGV = ["/opt/x/python", "-m", "envrec_mas", "--state-dir", "/s dir", "--log-file", "/s dir/service-node.log", "node", "run"]


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def test_darwin_writes_a_launch_agent_whose_arguments_are_the_program_line_and_loads_it_for_this_user(tmp_path):
    f = Fake()
    p = svc.plan("node", ARGV, family="darwin", runner=f, env={"HOME": str(tmp_path)})
    assert p.kind == "launchd-agent" and p.file == tmp_path / "Library/LaunchAgents/io.github.multi-auto-setup.node.plist"
    plist = plistlib.loads(p.content.encode())
    assert plist["ProgramArguments"] == ARGV and plist["RunAtLoad"] is True and plist["Label"] == p.label
    assert plist["KeepAlive"] == {"SuccessfulExit": False}  # a node that was unpaired exits 0 and is not restarted
    svc.install(p, runner=f)
    assert p.file.read_text() == p.content
    assert any(c[:2] == ["launchctl", "bootstrap"] and c[2].startswith("gui/") and c[3] == str(p.file) for c in f.calls)


def test_linux_prefers_systemd_user_and_writes_a_unit_with_restart_on_failure(tmp_path):
    p = svc.plan("node", ARGV, family="linux", runner=Fake(), env={"HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / "cfg")})
    assert p.kind == "systemd-user" and p.file == tmp_path / "cfg/systemd/user/mas-node.service"
    assert "ExecStart=/opt/x/python -m envrec_mas --state-dir '/s dir' --log-file '/s dir/service-node.log' node run" in p.content
    assert "Restart=on-failure" in p.content and "WantedBy=default.target" in p.content
    assert ["systemctl", "--user", "enable", "--now", "mas-node.service"] in p.start


def test_linux_without_systemd_user_falls_back_to_the_desktop_autostart_entry_only_with_a_desktop(tmp_path):
    no_systemd = Fake(absent={"systemctl"})
    p = svc.plan("node", ARGV, family="linux", runner=no_systemd, env={"HOME": str(tmp_path), "XDG_CURRENT_DESKTOP": "GNOME"})
    assert p.kind == "xdg-autostart" and p.file == tmp_path / ".config/autostart/mas-node.desktop" and "Exec=" in p.content and p.start == []
    with pytest.raises(svc.ServiceRefused) as e:  # a container or a bare server: no manager, no desktop
        svc.plan("node", ARGV, family="linux", runner=no_systemd, env={"HOME": str(tmp_path)})
    assert e.value.reason == "no_user_service_manager"


def test_windows_uses_the_current_users_run_key_a_windowless_python_and_quotes_paths_with_spaces(tmp_path):
    scripts = tmp_path / "My Venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "python.exe").write_text("")
    (scripts / "pythonw.exe").write_text("")
    argv = [str(scripts / "python.exe"), "-m", "envrec_mas", "--state-dir", r"C:\Users\A B\mas", "node", "run"]
    f = Fake()
    p = svc.plan("node", argv, family="windows", runner=f, env={})
    assert p.kind == "run-key" and p.file is None
    assert p.command.startswith(f'"{scripts / "pythonw.exe"}" -m envrec_mas') and '"C:\\Users\\A B\\mas"' in p.command
    add = p.start[0]
    assert add[:2] == ["reg", "add"] and "HKCU" in add[2] and "/f" in add and add[add.index("/d") + 1] == p.command and add[add.index("/v") + 1] == "MultiAutoSetup-node"
    assert not any("HKLM" in a or "schtasks" in a for c in p.start + p.stop for a in c)  # nothing that needs an administrator


@pytest.mark.parametrize("family", ["darwin", "linux", "windows"])
def test_no_family_writes_outside_the_profile_or_uses_elevation(family, tmp_path):
    p = svc.plan("node", ARGV, family=family, runner=Fake(), env={"HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / "c")})
    if p.file is not None:
        assert str(tmp_path) in str(p.file)
    forbidden = {"sudo", "runas", "HKLM", "/Library/LaunchDaemons", "enable-linger"}
    assert not any(w in " ".join(c) for c in p.pre + p.start + p.stop for w in forbidden)
    assert "How to undo it" in p.describe() and p.command in p.describe()


def test_a_failing_command_removes_what_install_wrote_and_says_which_command_failed(tmp_path):
    p = svc.plan("node", ARGV, family="darwin", runner=Fake(), env={"HOME": str(tmp_path)})
    with pytest.raises(svc.ServiceRefused) as e:
        svc.install(p, runner=Fake(fail={"bootstrap"}))
    assert e.value.reason == "command_failed" and "bootstrap" in e.value.text and not p.file.exists()


def test_uninstall_stops_removes_and_is_safe_to_repeat(tmp_path):
    f = Fake()
    p = svc.plan("node", ARGV, family="linux", runner=f, env={"HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / "c")})
    svc.install(p, runner=f)
    assert svc.installed(p, runner=f)
    svc.uninstall(p, runner=f)
    assert not p.file.exists() and not svc.installed(p, runner=f)
    assert ["systemctl", "--user", "disable", "--now", "mas-node.service"] in f.calls
    svc.uninstall(p, runner=f)  # again: nothing to remove, no error


def test_the_two_roles_get_different_names_and_an_unknown_role_is_refused(tmp_path):
    a = svc.plan("node", ARGV, family="darwin", runner=Fake(), env={"HOME": str(tmp_path)})
    b = svc.plan("coordinator", ARGV, family="darwin", runner=Fake(), env={"HOME": str(tmp_path)})
    assert a.file != b.file and a.label != b.label
    with pytest.raises(svc.ServiceRefused):
        svc.plan("admin", ARGV, family="darwin", runner=Fake(), env={})


def test_a_family_with_no_row_is_refused_with_the_typed_reason():
    with pytest.raises(svc.ServiceRefused) as e:
        svc.plan("node", ARGV, family="plan9", runner=Fake(), env={})
    assert e.value.reason == "no_user_service_manager"

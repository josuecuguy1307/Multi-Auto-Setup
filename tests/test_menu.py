"""The first-run menu (`mas` with no arguments). SIMULATED-LOCAL: a fake backend stands in for services, the chat and the network; nothing here touches this computer."""
import io
import re
from argparse import Namespace

import pytest

from envrec_mas import cli, lifecycle, menu
from envrec_mas import strings as S
from envrec_style import term

ANSI = re.compile(r"\x1b\[[0-9;]*m")


class Fake:
    """A fake machine: what is done, what runs, what the owner types."""

    def __init__(self, answers, *, role=None, done=(), banner=None, addresses=(("en0", "192.168.1.20"), ("en1", "10.0.0.5"))):
        self.answers, self.role, self.done_set, self.out, self.ran, self.prompts = list(answers), role, set(done), [], [], []
        self.addresses, self.banner_text = list(addresses), banner or "BANNER"

    def ports(self):
        def read(prompt):
            self.prompts.append(prompt)
            if not self.answers:
                raise EOFError
            return self.answers.pop(0)

        def run_cli(argv):
            self.ran.append(argv)
            if argv[0] == "service":
                self.done_set.add(("service", argv[argv.index("--role") + 1]))
            if argv[0] == "connect":
                self.done_set.add(("connect", "claude_desktop_connected"))
            if argv[0] == "join":
                self.done_set.add(("join", "joined"))
            return 0

        def done(role, check):
            return {"service_installed": ("service", role), "claude_desktop_connected": ("connect", check), "joined": ("join", check)}[check] in self.done_set

        return menu.Ports(read=read, write=self.out.append, run_cli=run_cli, done=done, detect_role=lambda: self.role, addresses=lambda: self.addresses, banner=lambda: self.banner_text)

    @property
    def text(self):
        return "\n".join(self.out)


def test_coordinator_flow_runs_the_existing_commands_and_never_passes_yes():
    f = Fake(["1", "1", "1", "2", ""])  # role coordinator; service step; first address; connect step; leave
    assert menu.run_menu(f.ports()) == 0
    assert f.ran == [["service", "install", "--role", "coordinator", "--bind", "192.168.1.20"], ["connect", "claude-desktop"]]
    assert not any("--yes" in a for argv in f.ran for a in argv)
    assert "mas service uninstall --role coordinator" in f.text and "mas disconnect claude-desktop" in f.text  # the undo is shown before the step runs
    assert S.MENU_RESTART_CLAUDE in f.text


def test_node_flow_waits_for_join_before_the_service():
    f = Fake(["2", "2", "1", "2", ""])  # node; the service (refused: join first); join; service; leave
    menu.run_menu(f.ports())
    assert f.ran == [["join"], ["service", "install", "--role", "node"]]
    assert S.NEEDS_NOTE in f.text


def test_a_rerun_shows_what_is_done_and_offers_only_what_is_missing():
    f = Fake([""], role="coordinator", done={("service", "coordinator")})
    menu.run_menu(f.ports())
    assert f.ran == []
    lines = [ln for ln in f.out if re.match(r"\s+\d\. \[", ln)]
    assert "[x]" in lines[0] and "(done)" in lines[0] and "[ ]" in lines[1]
    assert S.ROLE_FOUND.format(role="coordinator") in f.text and not any(S.ROLE_ASK in p for p in f.prompts)


def test_everything_done_ends_without_asking_anything():
    f = Fake([], role="node", done={("join", "joined"), ("service", "node")})
    assert menu.run_menu(f.ports()) == 0
    assert S.ALL_DONE in f.text and f.prompts == [] and f.ran == []


def test_leaving_or_no_answer_changes_nothing():
    for answers in ([], [""], ["x"]):
        f = Fake(answers)
        assert menu.run_menu(f.ports()) == 0
        assert f.ran == []


def test_no_network_address_cancels_the_coordinator_service():
    f = Fake(["1", "1", ""], addresses=())
    menu.run_menu(f.ports())
    assert f.ran == [] and S.ADDRESS_NONE in f.text


def test_a_bad_address_choice_runs_nothing():
    f = Fake(["1", "1", "9", ""])
    menu.run_menu(f.ports())
    assert f.ran == []


def test_a_failed_step_is_reported_and_not_marked_done():
    f = Fake(["2", "1", ""])
    p = f.ports()
    p.run_cli = lambda argv: 1
    menu.run_menu(p)
    assert S.STEP_RESULT_FAIL.format(code=1) in f.text


def test_menu_data_and_strings_agree():
    data = menu.load_menu()
    for role in data["roles"].values():
        for s in role["steps"]:
            assert s["label"] in S.STEP_LABELS and s["command"].startswith("mas ") and s["undo"].startswith("mas ")
            assert "--yes" not in s["command"]
            if s.get("then"):
                assert hasattr(S, s["then"].upper())
        ids = [s["id"] for s in role["steps"]]
        assert all(s["needs"] is None or s["needs"] in ids for s in role["steps"])
    for text in [getattr(S, n) for n in dir(S) if n.isupper() and isinstance(getattr(S, n), str)] + list(S.STEP_LABELS.values()):
        assert text.isascii(), text


def test_every_step_command_parses_with_the_real_command_line(monkeypatch):
    seen = []
    monkeypatch.setattr(lifecycle, "cmd_service", lambda a: seen.append(("service", a.action, a.role, a.bind, a.yes)) or 0)
    monkeypatch.setattr(cli, "cmd_connect", lambda a: seen.append(("connect", a.host)) or 0)
    monkeypatch.setattr(cli, "cmd_join", lambda a: seen.append(("join",)) or 0)
    for role in menu.load_menu()["roles"].values():
        for s in role["steps"]:
            assert cli.main(menu._argv(menu.command_text(s["command"], "192.168.1.20"))) == 0
    assert ("service", "install", "coordinator", "192.168.1.20", False) in seen and ("connect", "claude-desktop") in seen and ("join",) in seen
    assert ("service", "install", "node", None, False) in seen


def test_declining_the_real_service_prompt_writes_nothing(tmp_path, monkeypatch, capsys):
    """Through the real parser and the real `mas service install`: the owner types no, and the state folder stays empty."""
    monkeypatch.setattr("builtins.input", lambda prompt="": "no")
    code = cli.main(["--state-dir", str(tmp_path / "state")] + menu._argv(menu.command_text("mas service install --role coordinator --bind {bind}", "192.168.1.20")))
    assert code == 1 and "Nothing was changed." in capsys.readouterr().out
    assert not (tmp_path / "state" / "coordinator.json").exists()


def test_no_color_menu_has_no_escape_codes():
    f = Fake([""], role="node", banner=term.render(columns=80, tty=True, env={"NO_COLOR": "1"}))
    menu.run_menu(f.ports())
    assert "\x1b" not in f.text and "█" in f.text


def test_narrow_terminal_banner_fits():
    banner = ANSI.sub("", term.render(columns=24, tty=True, env={"TERM": "xterm-256color"}))
    assert max(len(ln) for ln in banner.split("\n")) <= 24


def test_not_a_terminal_prints_help_and_never_prompts(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("the menu must not ask without a terminal"))
    code = cli.main(["--state-dir", "/nonexistent-mas-state"])
    out = capsys.readouterr().out
    assert code == 2 and "\x1b" not in out and "usage: mas" in out and "MAS - Multi Auto Setup" in out


def test_main_for_terminal_refuses_unless_both_ends_are_terminals(tmp_path):
    class Tty(io.StringIO):
        def isatty(self):
            return True
    assert menu.main_for_terminal(tmp_path, [], stdin=io.StringIO(), stdout=Tty()) == 2
    assert menu.main_for_terminal(tmp_path, [], stdin=Tty(), stdout=io.StringIO()) == 2
    assert not list(tmp_path.iterdir())


def test_detect_role_reads_disk_and_writes_nothing(tmp_path):
    ports = menu.real_ports(tmp_path, [])
    assert ports.detect_role() is None
    (tmp_path / "coordinator").mkdir()
    assert ports.detect_role() == "coordinator"

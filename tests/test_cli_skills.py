"""D11: the CLI over the skill library, the status view, the guided connect and the teardown."""

from __future__ import annotations

import pytest

from envrec.cli import main
from envrec.skills.sbx import docker_ready


def run(capsys, *argv):
    code = main(list(argv))
    return code, capsys.readouterr().out


def test_skills_list_shows_every_kind_and_names_what_was_written_by_a_model(capsys):
    code, out = run(capsys, "skills", "list")
    assert code == 0 and "usb-devices" in out and "protocol-generic" in out and "authoring" in out and "0 written by a model" in out
    assert "usb-devices" in run(capsys, "skills", "list", "--kind", "skill")[1] and "protocol-host" not in run(capsys, "skills", "list", "--kind", "skill")[1]
    assert "protocol-host" in run(capsys, "skills", "list", "--kind", "protocol")[1]


def test_skills_show_prints_the_plan_the_tests_and_the_start_of_the_body(capsys):
    code, out = run(capsys, "skills", "show", "serial-probe", "--lines", "3")
    assert code == 0 and "plan:" in out and "serial_identify" in out and "tests: 3" in out and "# Identify one serial port" in out
    assert run(capsys, "skills", "show", "protocol-usb")[1].count(">") >= 4  # a protocol shows its steps
    assert main(["skills", "show", "no-such-skill"]) == 2


@pytest.mark.skipif(not docker_ready(), reason="verify runs the skill's tests in the sandbox")
def test_skills_verify_runs_the_checks_and_the_tests_and_records_a_device_only_on_success(capsys, tmp_path):
    code, out = run(capsys, "skills", "verify", "net-listening")
    assert code == 0 and "VERIFIED" in out and "test macos: pass" in out


def test_status_reports_the_pieces_without_failing_when_one_is_missing(capsys, tmp_path):
    code, out = run(capsys, "status", "--targets", str(tmp_path / "none.yaml"))
    assert code == 0 and "skills:" in out and "sandbox:" in out and "tools:" in out and "no targets.yaml" in out


def test_connect_refuses_a_device_that_is_not_enrolled_and_changes_nothing(capsys, tmp_path):
    t = tmp_path / "targets.yaml"
    t.write_text("targets: []\n")
    code = main(["connect", "--target", "ghost", "--targets", str(t)])
    assert code == 2 and "not an entry" in capsys.readouterr().err


def test_teardown_without_a_target_says_what_it_needs(capsys):
    code, out = run(capsys, "teardown")
    assert code == 0 and "nothing to tear down" in out

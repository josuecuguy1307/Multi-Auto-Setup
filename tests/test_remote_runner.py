import pytest

from envrec.contracts import Tier
from envrec.mission import DockerExecBlock, EntryLimits, SshBlock, TargetConfig, TargetEntry, Transport
from envrec.remote.runner import MissingKey, RemoteRunner, TargetNotAllowed
from envrec.remote.templates import INSTALL_ROOT, TEMPLATES, TemplateError, get_template


def entry(**kw):
    base = dict(id="lab-b", name="Lab B", host="192.168.1.42", allowed_transports=[Transport.ssh, Transport.docker_exec],
                ssh=SshBlock(user="josue", port=2222, key_env="LAB_B_KEY_PATH"), docker_exec=DockerExecBlock(container="lab-b"),
                limits=EntryLimits(max_disk_gb=20, max_model_params="9B"), owner_confirmed=True)
    return TargetEntry(**{**base, **kw})


class Recorder:
    def __init__(self, result=(0, b"ok", b"")):
        self.calls, self.result = [], result

    def __call__(self, argv, stdin, timeout):
        self.calls.append((argv, stdin))
        return self.result


@pytest.fixture
def runner(monkeypatch):
    monkeypatch.setenv("LAB_B_KEY_PATH", "/keys/secret_test_key")
    rec = Recorder()
    return RemoteRunner(TargetConfig(targets=[entry()]), exec_fn=rec), rec


def test_a_host_not_in_targets_yaml_raises_before_any_process_starts(runner):
    r, rec = runner
    with pytest.raises(TargetNotAllowed, match="not an entry"):
        r.run("some-other-host", Transport.ssh, "uname_m")
    with pytest.raises(TargetNotAllowed):
        r.run("192.168.1.42", Transport.ssh, "uname_m")  # an address is not an id
    assert rec.calls == [] and r.attempts == []


def test_a_transport_the_entry_does_not_list_is_refused(runner):
    r, rec = runner
    r2 = RemoteRunner(TargetConfig(targets=[entry(allowed_transports=[Transport.ssh], docker_exec=None)]), exec_fn=rec)
    with pytest.raises(TargetNotAllowed, match="not an allowed transport"):
        r2.run("lab-b", Transport.docker_exec, "uname_m")
    assert rec.calls == []


def test_transports_without_a_runner_in_this_phase_are_refused():
    from envrec.mission import SerialBlock
    cfg = TargetConfig(targets=[entry(allowed_transports=[Transport.serial], ssh=None, docker_exec=None,
                                      serial=SerialBlock(path="/dev/ttyUSB0"))])
    rec = Recorder()
    with pytest.raises(TargetNotAllowed, match="no runner is built"):
        RemoteRunner(cfg, exec_fn=rec).run("lab-b", Transport.serial, "uname_m")
    assert rec.calls == []


def test_ssh_command_shape_and_the_key_path_never_leaks_into_the_result(runner):
    r, rec = runner
    res = r.run("lab-b", Transport.ssh, "df_free", {"path": "/opt"})
    argv, _ = rec.calls[0]
    assert argv[0] == "ssh" and "/keys/secret_test_key" in argv and argv[-1] == "df -Pk /opt"
    assert "josue@192.168.1.42" in argv and argv[argv.index("-p") + 1] == "2222"
    assert "BatchMode=yes" in argv and "StrictHostKeyChecking=yes" in argv
    assert "/keys/secret_test_key" not in " ".join(res.argv_shown) and "$LAB_B_KEY_PATH" in " ".join(res.argv_shown)
    assert "secret_test_key" not in repr(res)


def test_a_missing_key_variable_fails_before_a_connection_and_never_prints_a_path(monkeypatch):
    monkeypatch.delenv("LAB_B_KEY_PATH", raising=False)
    rec = Recorder()
    with pytest.raises(MissingKey, match="LAB_B_KEY_PATH"):
        RemoteRunner(TargetConfig(targets=[entry()]), exec_fn=rec).run("lab-b", Transport.ssh, "uname_m")
    assert rec.calls == []


def test_docker_exec_uses_the_configured_container_only(runner):
    r, rec = runner
    r.run("lab-b", Transport.docker_exec, "uname_m")
    assert rec.calls[0][0] == ["docker", "exec", "-i", "lab-b", "uname", "-m"]


def test_template_substitution_rejects_shell_metacharacters_and_escapes():
    t = get_template("remove_tree")
    for bad in (f"{INSTALL_ROOT}/x; rm -rf /", f"{INSTALL_ROOT}/$(id)", f"{INSTALL_ROOT}/`id`", f"{INSTALL_ROOT}/a b",
                f"{INSTALL_ROOT}/../etc", "/etc", "/", f"{INSTALL_ROOT}/a\nb", f"{INSTALL_ROOT}//x", "relative/path",
                f"{INSTALL_ROOT}/a|b", f"{INSTALL_ROOT}/a>b", f"{INSTALL_ROOT}/a'b"):
        with pytest.raises(TemplateError):
            t.render({"dir": bad})
    assert t.render({"dir": f"{INSTALL_ROOT}/worker-1.0"}) == ["rm", "-rf", f"{INSTALL_ROOT}/worker-1.0"]


def test_ports_names_and_hashes_are_validated():
    s = get_template("start_server")
    good = dict(runtime=f"{INSTALL_ROOT}/rt.pyz", port=8089, model="tiny-0.5b", log=f"{INSTALL_ROOT}/s.log", pidfile=f"{INSTALL_ROOT}/s.pid")
    assert s.render(good)[-5:] == [f"{INSTALL_ROOT}/rt.pyz", "8089", "tiny-0.5b", f"{INSTALL_ROOT}/s.log", f"{INSTALL_ROOT}/s.pid"]
    for key, bad in (("port", 80), ("port", "8089; ls"), ("port", 70000), ("model", "a;b"), ("model", "../x"), ("model", "x" * 80)):
        with pytest.raises(TemplateError):
            s.render({**good, key: bad})
    with pytest.raises(TemplateError):
        get_template("sha256_file").render({"path": "x"})


def test_unknown_missing_and_unlisted_placeholders_are_errors():
    with pytest.raises(TemplateError, match="needs"):
        get_template("remove_tree").render({})
    with pytest.raises(TemplateError, match="no placeholder"):
        get_template("remove_tree").render({"dir": f"{INSTALL_ROOT}/x", "extra": "y"})
    with pytest.raises(TemplateError, match="no template named"):
        get_template("rm_everything")


def test_values_reach_scripts_only_as_positional_parameters_never_in_script_text():
    for tpl in TEMPLATES.values():
        if tpl.argv[0] == "sh" and tpl.argv[1] == "-c":
            script, params = tpl.argv[2], tpl.argv[3:]
            assert "{" not in script.replace("{ echo", "").replace("; }", ""), f"{tpl.name}: placeholder inside script text"
            assert all("{" in p or p == "sh" for p in params if p != "sh"), tpl.name
        else:
            assert tpl.argv[0] != "sh"


def test_every_template_that_changes_things_needs_approval_and_stdin_is_only_for_transfers():
    writers = {n for n, t in TEMPLATES.items() if t.tier != Tier.observe}
    assert writers == {"mkdir_install", "receive_file", "chmod_exec", "start_server", "stop_server", "remove_tree"}
    assert all(TEMPLATES[n].tier == Tier.write_approval for n in writers)
    assert {n for n, t in TEMPLATES.items() if t.sends_stdin} == {"receive_file", "chat_request"}


def test_stdin_is_refused_for_templates_that_do_not_take_it(runner):
    r, rec = runner
    with pytest.raises(TemplateError):
        r.run("lab-b", Transport.ssh, "uname_m", stdin=b"hi")
    assert rec.calls == []


def test_a_child_process_cannot_read_the_humans_terminal_input():
    """Regression: `docker exec -i` swallowed the approval answers piped to the controller's stdin."""
    import subprocess
    import sys
    code = ("from envrec.remote.runner import _real_exec\n"
            "code, out, err = _real_exec(['cat'], None, 5)\n"
            "print('child saw:', out)\n"
            "print('parent still has:', input())")
    r = subprocess.run([sys.executable, "-c", code], input="approve\n", capture_output=True, text=True, timeout=30)
    assert "child saw: b''" in r.stdout and "parent still has: approve" in r.stdout, r.stdout + r.stderr

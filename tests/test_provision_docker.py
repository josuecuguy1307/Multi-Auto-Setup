"""Provisioning against real containers: sshd + python (envrec-fixture-sshd) and a bare box (envrec-fixture-nopy).

The inference server is the fake llama-server (tests/fakes), packed as a runtime archive; the flow is the real one:
preflight, transfer of worker + runtime archive + model file with a fresh approval each, unpack, start through the
worker's supervisor, health, tunnel, verification, one-command undo.
"""

import json
import sys
from pathlib import Path

import pytest

from docker_helpers import container, exec_in, free_port, make_keypair, need_docker, wait_ssh
from envrec.contracts import FactStatus
from envrec.mission import DockerExecBlock, EntryLimits, SshBlock, TargetConfig, TargetEntry, Transport
from envrec.remote.approval import ApprovalGate, UndoLog
from envrec.remote.provision import ROOT, SERVER_PORT, ChecksumMismatch, Provisioner, Release
from envrec.remote.runner import RemoteRunner
from envrec.store import Store
from envrec.verify import ExecClient, TunnelClient, verify_server
from runtime_fixtures import fake_plan

ROOT_DIR = Path(__file__).resolve().parent.parent
pytestmark = need_docker("envrec-fixture-sshd", "envrec-fixture-nopy")
APPROVE = ["approve"] * 30
DEPLOY_WRITES = 10  # mkdir x3, worker, worker targets.yaml, runtime archive, mkdir + unpack, model, start


@pytest.fixture(scope="module")
def release(tmp_path_factory):
    sys.path.insert(0, str(ROOT_DIR / "build" / "worker"))
    import build as worker_build
    out = tmp_path_factory.mktemp("dist")
    built = worker_build.build(out)
    pinned = json.loads((ROOT_DIR / "build" / "worker" / "release.json").read_text())
    assert built["sha256"] == pinned["sha256"], "worker build is not reproducible or release.json is stale: run build/worker/build.py --write-release"
    return Release.load(ROOT_DIR / "build" / "worker" / "release.json", out)


@pytest.fixture(scope="module")
def plan(tmp_path_factory):
    return fake_plan(tmp_path_factory.mktemp("art"), threads=2, context=512, max_rss_mib=800, min_free_disk_mb=200)


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "s") as s:
        yield s


class Answers:
    def __init__(self, answers):
        self.answers, self.shown = list(answers), []

    def ask(self, prompt):
        return self.answers.pop(0) if self.answers else "deny"

    def show(self, text):
        self.shown.append(text)


def exec_entry(container_name, **kw):
    base = dict(id="box-1", name="Box 1", host="127.0.0.1", allowed_transports=[Transport.docker_exec],
                docker_exec=DockerExecBlock(container=container_name),
                limits=EntryLimits(max_disk_gb=20, max_model_params="13B"), owner_confirmed=True)
    return TargetEntry(**{**base, **kw})


def provisioner(store, release, entry, answers, exec_fn=None, **runner_kw):
    cfg = TargetConfig(targets=[entry])
    h = Answers(answers)
    kwargs = {"exec_fn": exec_fn} if exec_fn else {}
    runner = RemoteRunner(cfg, **kwargs, **runner_kw)
    return Provisioner(runner, ApprovalGate(store, h.ask, h.show), store, cfg, release), h


def listening_on_localhost_only(name, port):
    out = exec_in(name, "cat", "/proc/net/tcp").stdout.splitlines()[1:]
    mine = [r.split()[1] for r in out if r.split()[3] == "0A" and int(r.split()[1].split(":")[1], 16) == port]
    return mine == [f"0100007F:{port:04X}"]


# ---- the whole flow over a real SSH connection, including the tunnel -------------------------------------------------

@pytest.fixture(scope="module")
def ssh_box(tmp_path_factory):
    d = tmp_path_factory.mktemp("ssh")
    key, pub = make_keypair(d)
    port = free_port()
    known = str(d / "known_hosts")
    with container("envrec-fixture-sshd", "-m", "2g", "-p", f"127.0.0.1:{port}:22", env={"PUBKEY": pub}) as name:
        wait_ssh(port, key, known)
        yield {"name": name, "port": port, "key": key, "known": known}


def ssh_setup(store, release, box, monkeypatch, answers):
    monkeypatch.setenv("TEST_SSH_KEY", box["key"])
    entry = TargetEntry(id="ssh-box", name="SSH Box", host="127.0.0.1", allowed_transports=[Transport.ssh],
                        ssh=SshBlock(user="josue", port=box["port"], key_env="TEST_SSH_KEY"),
                        limits=EntryLimits(max_disk_gb=20, max_model_params="13B"), owner_confirmed=True)
    return provisioner(store, release, entry, answers, host_key_policy="accept-new", known_hosts=box["known"])


def test_full_flow_over_ssh_with_a_localhost_only_server_a_tunnel_a_verifier_and_one_command_undo(store, release, plan, ssh_box, monkeypatch):
    p, human = ssh_setup(store, release, ssh_box, monkeypatch, APPROVE)
    res = p.deploy("ssh-box", Transport.ssh, plan)
    assert res.ok, res.failure
    assert p.gate.asked == DEPLOY_WRITES
    assert listening_on_localhost_only(ssh_box["name"], SERVER_PORT)  # 127.0.0.1 only, not 0.0.0.0
    assert exec_in(ssh_box["name"], "test", "-x", res.facts["binary"]).returncode == 0  # the unpacked server is executable

    tunnel = p.open_tunnel("ssh-box")  # one more approved action
    try:
        out = verify_server(TunnelClient(tunnel.local_port), store, p.targets["ssh-box"])
        assert out.ok, out.reason
    finally:
        tunnel.close()
    fact = store.current_fact(p.targets["ssh-box"].id, "host.model_server")
    assert fact.status == FactStatus.verified and fact.created_by.value == "verifier"
    assert ssh_box["key"] not in repr(store.events())  # the key path never lands in the log

    ok, msg = p.undo("ssh-box", Transport.ssh)
    assert ok, msg
    assert "port 8089 closed" in msg
    assert exec_in(ssh_box["name"], "test", "-e", ROOT).returncode != 0  # runtime and model files are gone
    assert exec_in(ssh_box["name"], "pgrep", "-f", "llama-server").returncode != 0  # and nothing runs


def test_the_server_is_not_reachable_from_outside_the_device(store, release, plan, ssh_box, monkeypatch):
    import socket
    p, _ = ssh_setup(store, release, ssh_box, monkeypatch, APPROVE)
    assert p.deploy("ssh-box", Transport.ssh, plan).ok
    try:
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", SERVER_PORT), timeout=1).close()  # the port is not published
    finally:
        p.undo("ssh-box", Transport.ssh)


def test_a_host_not_in_targets_yaml_is_never_contacted_even_with_a_matching_container(store, release, plan, ssh_box, monkeypatch):
    p, _ = ssh_setup(store, release, ssh_box, monkeypatch, APPROVE)
    with pytest.raises(Exception, match="not an entry"):
        p.deploy("someone-elses-box", Transport.ssh, plan)
    assert p.runner.attempts == []


# ---- docker_exec containers: approvals, failures, sabotage -------------------------------------------------------------

def exec_box(mem="2g", extra=()):
    return container("envrec-fixture-sshd", "-m", mem, "--entrypoint", "sleep", *extra, command=["infinity"])


def deploy_it(store, release, plan, name, answers=APPROVE, exec_fn=None):
    p, human = provisioner(store, release, exec_entry(name), answers, exec_fn)
    return p, human, p.deploy("box-1", Transport.docker_exec, plan)


def undo(p):
    p.gate.ask = lambda prompt: "approve"
    return p.undo("box-1", Transport.docker_exec)


@pytest.mark.parametrize(("mem", "rss_cap"), [("1g", 870), ("2g", 1740), ("4g", 3481)])
def test_the_memory_cap_follows_the_devices_real_ram_across_three_tiers(store, release, plan, mem, rss_cap):
    with exec_box(mem) as name:
        p, _, res = deploy_it(store, release, plan, name)
        assert res.ok, res.failure
        assert p.caps_for(res.preflight, exec_entry(name), 512)["max_rss_mib"] == rss_cap  # 85% of the container's own limit
        v = verify_server(ExecClient(p.runner, "box-1", Transport.docker_exec, SERVER_PORT), store, p.targets["box-1"])
        assert v.ok, v.reason
        ok, msg = undo(p)
        assert ok and exec_in(name, "test", "-e", ROOT).returncode != 0


def test_every_write_asks_a_fresh_question_and_nothing_is_cached(store, release, plan):
    with exec_box() as name:
        p, human, res = deploy_it(store, release, plan, name)
        assert res.ok
        assert p.gate.asked == DEPLOY_WRITES and sum("APPROVAL REQUIRED" in s for s in human.shown) == DEPLOY_WRITES
        assert all(e.payload["granted"] for e in store.events(type="approval"))
        before = p.gate.asked
        undo(p)
        assert p.gate.asked == before + 1  # undo is one more approved command, asked fresh


def test_the_approval_prompts_show_command_target_checksum_origin_size_and_version(store, release, plan):
    with exec_box() as name:
        _, human, _ = deploy_it(store, release, plan, name)
        undo_p = None
    worker = next(s for s in human.shown if "dd of=" in s and release.filename in s)
    for needle in ("recipe:  transfer_model", "target:  box-1", "origin:  builtin", release.sha256, release.version, "dd of=", " MB"):
        assert needle in worker, needle
    runtime = next(s for s in human.shown if plan.runtime.file in s and "dd of=" in s)
    model = next(s for s in human.shown if plan.model.file in s and "dd of=" in s)
    assert plan.runtime.sha256 in runtime and plan.model.sha256 in model  # artifact name, size and sha256 for each push


def test_yes_to_one_action_is_not_yes_to_the_next(store, release, plan):
    with exec_box() as name:
        p, _, res = deploy_it(store, release, plan, name, answers=["approve"])
        assert not res.ok and res.failure == "denied by the human"
        assert exec_in(name, "test", "-d", ROOT).returncode == 0
        assert exec_in(name, "test", "-e", f"{ROOT}/models").returncode != 0


def test_a_denied_first_approval_leaves_no_partial_state(store, release, plan):
    with exec_box() as name:
        p, _, res = deploy_it(store, release, plan, name, answers=["deny"])
        assert not res.ok and UndoLog(store, "box-1").pending() == []
        assert exec_in(name, "test", "-e", ROOT).returncode != 0


def test_a_denial_midway_is_fully_covered_by_the_undo_log(store, release, plan):
    with exec_box() as name:
        p, _, res = deploy_it(store, release, plan, name, answers=["approve"] * 6 + ["deny"])
        assert not res.ok and res.needs_undo and UndoLog(store, "box-1").pending()
        assert exec_in(name, "test", "-e", ROOT).returncode == 0
        ok, msg = undo(p)
        assert ok, msg
        assert exec_in(name, "test", "-e", ROOT).returncode != 0 and UndoLog(store, "box-1").pending() == []


def test_a_denied_undo_changes_nothing(store, release, plan):
    with exec_box() as name:
        p, _, res = deploy_it(store, release, plan, name)
        p.gate.ask = lambda prompt: "no"
        ok, msg = p.undo("box-1", Transport.docker_exec)
        assert not ok and "unchanged" in msg and exec_in(name, "test", "-e", ROOT).returncode == 0
        undo(p)


def test_a_read_only_filesystem_is_a_clean_honest_failure(store, release, plan):
    with exec_box(extra=("--read-only", "--tmpfs", "/tmp")) as name:
        _, _, res = deploy_it(store, release, plan, name)
        assert not res.ok and res.steps[-1].status == "failed" and "Read-only file system" in res.failure
        assert not any(s.recipe in ("start_server", "health_check") for s in res.steps)  # no invented success


def test_an_offline_box_without_python_fails_with_a_hint_and_installs_nothing(store, release, plan):
    with container("envrec-fixture-nopy", "--network", "none", "-m", "2g") as name:
        p, _, res = deploy_it(store, release, plan, name)
        assert not res.ok and res.steps[-1].recipe == "install_runtime" and "python3 is missing" in res.failure
        assert "installs no system packages" in res.failure
        assert p.gate.asked == 0 and exec_in(name, "test", "-e", ROOT).returncode != 0


def test_an_air_gapped_box_receives_everything_from_the_controller_alone(store, release, plan):
    with exec_box(extra=("--network", "none")) as name:
        net = exec_in(name, "python3", "-c", "import urllib.request;urllib.request.urlopen('http://1.1.1.1',timeout=3)")
        assert net.returncode != 0  # the box really has no network
        p, _, res = deploy_it(store, release, plan, name)
        assert res.ok, res.failure
        v = verify_server(ExecClient(p.runner, "box-1", Transport.docker_exec, SERVER_PORT), store, p.targets["box-1"])
        assert v.ok, v.reason
        assert undo(p)[0]


def test_a_truncated_transfer_is_caught_by_the_checksum_and_leaves_nothing_after_undo(store, release, plan):
    from envrec.remote.runner import _real_exec

    def cut(argv, stdin, timeout):
        if isinstance(stdin, Path) and stdin.name == plan.model.file:
            stdin = stdin.read_bytes()[: plan.model.bytes // 2]  # the link dies half way through the model
        return _real_exec(argv, stdin, timeout)

    with exec_box() as name:
        p, _, res = deploy_it(store, release, plan, name, exec_fn=cut)
        assert not res.ok and "checksum mismatch on the device" in res.failure and res.needs_undo
        assert not any(s.recipe in ("start_server", "health_check") for s in res.steps)
        ok, msg = undo(p)
        assert ok and exec_in(name, "test", "-e", ROOT).returncode != 0


def test_a_killed_transfer_process_is_a_recorded_failure_not_a_success(store, release, plan):
    from envrec.remote.runner import _real_exec

    def killed(argv, stdin, timeout):
        return (137, b"", b"killed") if stdin is not None and "dd" in argv and "of=" + f"{ROOT}/runtime/{plan.runtime.file}" in argv else _real_exec(argv, stdin, timeout)

    with exec_box() as name:
        p, _, res = deploy_it(store, release, plan, name, exec_fn=killed)
        assert not res.ok and "transfer failed" in res.failure
        assert undo(p)[0] and exec_in(name, "test", "-e", ROOT).returncode != 0


def test_lying_ram_the_stricter_cgroup_limit_is_used(store, release, plan):
    with exec_box("2g") as name:
        p, _, res = deploy_it(store, release, plan, name)
        pf = res.preflight
        assert pf.ram_reported_gib > 5 and pf.ram_limit_gib == 2.0 and pf.ram_effective_gib == 2.0
        from envrec.remote.sizing import ram_cap_params_b
        assert ram_cap_params_b(pf.ram_reported_gib) > ram_cap_params_b(pf.ram_effective_gib)
        facts = {f.key: f.value for f in store.current_facts(p.targets["box-1"].id)}
        assert facts["hw.ram_total_gib"] == 2.0 and facts["hw.ram_host_reported_gib"] == pf.ram_reported_gib
        undo(p)


def test_a_tampered_artifact_aborts_before_any_approval_or_connection(store, release, plan, tmp_path):
    import shutil
    bad = tmp_path / "dist"
    shutil.copytree(release.dir, bad)
    (bad / release.filename).write_bytes((bad / release.filename).read_bytes() + b"tamper")
    bad_release = Release(release.version, release.filename, release.sha256, bad)
    with exec_box() as name:
        p, _ = provisioner(store, bad_release, exec_entry(name), APPROVE)
        with pytest.raises(ChecksumMismatch):
            p.deploy("box-1", Transport.docker_exec, plan)
        assert p.gate.asked == 0 and p.runner.attempts == [] and exec_in(name, "test", "-e", ROOT).returncode != 0


def test_a_tampered_cached_model_aborts_before_any_approval(store, release, tmp_path):
    local = fake_plan(tmp_path / "art")
    local.model_path.write_bytes(local.model_path.read_bytes() + b"x")
    with exec_box() as name:
        p, _ = provisioner(store, release, exec_entry(name), APPROVE)
        with pytest.raises(ChecksumMismatch):
            p.deploy("box-1", Transport.docker_exec, local)
        assert p.gate.asked == 0 and p.runner.attempts == []


def test_a_server_that_dies_right_after_start_is_a_recorded_failure(store, release, tmp_path):
    crash = fake_plan(tmp_path / "art")
    # a runtime whose server exits immediately: the supervisor reports it and the deploy must NOT claim success
    from runtime_fixtures import FAKES, make_runtime_archive, artifact
    import dataclasses
    exit_script = tmp_path / "exit_now.py"
    exit_script.write_text("#!/usr/bin/env python3\nimport sys\nprint('fatal: cannot load model', file=sys.stderr)\nsys.exit(3)\n")
    arc = make_runtime_archive(tmp_path / "bad", exit_script, name="bad.tar.gz")
    bad = dataclasses.replace(crash, runtime=artifact(arc, "runtime", "bad-runtime"), runtime_path=arc)
    with exec_box() as name:
        p, _, res = deploy_it(store, release, bad, name)
        assert not res.ok and "did not become healthy" in res.failure and "cannot load model" in res.failure
        assert res.steps[-1].status == "failed" and not any(s.recipe == "health_check" and s.status == "ok" for s in res.steps)
        assert undo(p)[0] and exec_in(name, "test", "-e", ROOT).returncode != 0


def test_a_server_killed_after_a_good_start_is_reported_as_crashed_never_running(store, release, plan):
    with exec_box() as name:
        p, _, res = deploy_it(store, release, plan, name)
        assert res.ok
        exec_in(name, "pkill", "-9", "-f", "llama-server")
        import time
        time.sleep(1.5)
        st = json.loads(p.runner.run("box-1", Transport.docker_exec, "runtime_status", {"worker": res.facts["worker"], "root": ROOT}).stdout)
        assert st["state"] in ("crashed", "stopped_by_watchdog", "failed") and st["state"] != "running"
        v = verify_server(ExecClient(p.runner, "box-1", Transport.docker_exec, SERVER_PORT), store, p.targets["box-1"])
        assert not v.ok and store.current_fact(p.targets["box-1"].id, "host.model_server") is None
        assert undo(p)[0]

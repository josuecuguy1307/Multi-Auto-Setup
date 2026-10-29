"""Provisioning against real containers: sshd + python (envrec-fixture-sshd) and a bare box (envrec-fixture-nopy)."""

import json
from pathlib import Path

import pytest

from docker_helpers import container, exec_in, free_port, make_keypair, need_docker, wait_ssh
from envrec.contracts import FactStatus, Target, TargetKind
from envrec.mission import DockerExecBlock, EntryLimits, SshBlock, TargetConfig, TargetEntry, Transport
from envrec.remote.approval import ApprovalGate, UndoLog
from envrec.remote.provision import ROOT, SERVER_PORT, ChecksumMismatch, Provisioner, Release
from envrec.remote.runner import RemoteRunner
from envrec.store import Store
from envrec.verify import ExecClient, TunnelClient, verify_server

ROOT_DIR = Path(__file__).resolve().parent.parent
pytestmark = need_docker("envrec-fixture-sshd", "envrec-fixture-nopy")


@pytest.fixture(scope="module")
def release(tmp_path_factory):
    import sys
    sys.path.insert(0, str(ROOT_DIR / "build" / "worker"))
    import build as worker_build
    out = tmp_path_factory.mktemp("dist")
    built = worker_build.build(out)
    pinned = json.loads((ROOT_DIR / "build" / "worker" / "release.json").read_text())
    assert built["sha256"] == pinned["sha256"], "worker build is not reproducible or release.json is stale: run build/worker/build.py --write-release"
    return Release.load(ROOT_DIR / "build" / "worker" / "release.json", out)


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "s") as s:
        yield s


class Answers:
    """Scripted human. Records every prompt so tests can check nothing was cached."""

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


APPROVE = ["approve"] * 20


def listening_on_localhost_only(name, port):
    """Read /proc/net/tcp inside the container: is the port bound to 127.0.0.1 and not 0.0.0.0?"""
    out = exec_in(name, "cat", "/proc/net/tcp").stdout.splitlines()[1:]
    rows = [ln.split() for ln in out if ln.split()[3] == "0A"]  # LISTEN
    mine = [r[1] for r in rows if int(r[1].split(":")[1], 16) == port]
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


def test_full_flow_over_ssh_with_a_localhost_only_server_a_tunnel_a_verifier_and_one_command_undo(store, release, ssh_box, monkeypatch):
    p, human = ssh_setup(store, release, ssh_box, monkeypatch, APPROVE)
    res = p.deploy("ssh-box", Transport.ssh)
    assert res.ok, res.failure
    assert res.model_name == "stub-1.5b" and res.model.limited_by in ("ram", "none")  # 2 GiB container
    assert human.shown and all("sha256" in s for s in human.shown if "receive" in s or "dd of=" in s)
    assert listening_on_localhost_only(ssh_box["name"], SERVER_PORT)  # 127.0.0.1 only, not 0.0.0.0

    tunnel = p.open_tunnel("ssh-box")  # one more approved action
    try:
        out = verify_server(TunnelClient(tunnel.local_port), store, p.targets["ssh-box"])
        assert out.ok, out.reason
    finally:
        tunnel.close()
    fact = store.current_fact(p.targets["ssh-box"].id, "host.model_server")
    assert fact.status == FactStatus.verified and fact.created_by.value == "verifier"
    assert "/home" not in repr(store.events()) and ssh_box["key"] not in repr(store.events())  # the key path never lands in the log

    ok, msg = p.undo("ssh-box", Transport.ssh)
    assert ok, msg
    assert exec_in(ssh_box["name"], "test", "-e", ROOT).returncode != 0  # nothing remains
    assert exec_in(ssh_box["name"], "pgrep", "-f", "envrec-worker").returncode != 0  # and nothing runs


def test_the_server_is_not_reachable_from_outside_the_device(store, release, ssh_box, monkeypatch):
    import socket
    p, _ = ssh_setup(store, release, ssh_box, monkeypatch, APPROVE)
    assert p.deploy("ssh-box", Transport.ssh).ok
    try:
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", SERVER_PORT), timeout=1).close()  # the port is not published
    finally:
        p.undo("ssh-box", Transport.ssh)


def test_a_host_not_in_targets_yaml_is_never_contacted_even_with_a_matching_container(store, release, ssh_box, monkeypatch):
    p, _ = ssh_setup(store, release, ssh_box, monkeypatch, APPROVE)
    with pytest.raises(Exception, match="not an entry"):
        p.deploy("someone-elses-box", Transport.ssh)
    assert p.runner.attempts == []


# ---- docker_exec containers: three RAM tiers, approvals, failures, sabotage -------------------------------------------

def exec_box(mem="2g", extra=()):
    return container("envrec-fixture-sshd", "-m", mem, "--entrypoint", "sleep", *extra, command=["infinity"])


def deploy_and_verify(store, release, name, answers=APPROVE, exec_fn=None):
    p, human = provisioner(store, release, exec_entry(name), answers, exec_fn)
    res = p.deploy("box-1", Transport.docker_exec)
    return p, human, res


@pytest.mark.parametrize(("mem", "model"), [("1g", "stub-0.5b"), ("2g", "stub-1.5b"), ("4g", "stub-3b")])
def test_three_ram_tiers_get_the_model_size_the_table_says(store, release, mem, model):
    with exec_box(mem) as name:
        p, _, res = deploy_and_verify(store, release, name)
        assert res.ok, res.failure
        assert res.model_name == model
        v = verify_server(ExecClient(p.runner, "box-1", Transport.docker_exec, SERVER_PORT), store, p.targets["box-1"])
        assert v.ok, v.reason
        assert p.undo("box-1", Transport.docker_exec)[0]
        assert exec_in(name, "test", "-e", ROOT).returncode != 0


def test_every_write_asks_a_fresh_question_and_nothing_is_cached(store, release):
    with exec_box() as name:
        p, human, res = deploy_and_verify(store, release, name)
        assert res.ok
        # mkdir x2, worker, worker targets.yaml, model, start_server = 6 separate approvals
        assert p.gate.asked == 6 and sum("APPROVAL REQUIRED" in s for s in human.shown) == 6
        grants = [e for e in store.events(type="approval")]
        assert len(grants) == 6 and all(e.payload["granted"] for e in grants)
        before = p.gate.asked
        p.undo("box-1", Transport.docker_exec)
        assert p.gate.asked == before + 1  # undo is one more approved command, asked fresh


def test_the_approval_prompt_shows_command_target_checksum_origin_and_version(store, release):
    with exec_box() as name:
        _, human, _ = deploy_and_verify(store, release, name, answers=["approve"] * 3 + ["deny"])
    transfer = next(s for s in human.shown if "dd of=" in s and release.filename in s)
    for needle in ("recipe:  transfer_model", "target:  box-1", "origin:  builtin", release.sha256, release.version, "dd of="):
        assert needle in transfer, needle


def test_yes_to_one_action_is_not_yes_to_the_next(store, release):
    with exec_box() as name:
        p, _, res = deploy_and_verify(store, release, name, answers=["approve"])  # only the first is approved
        assert not res.ok and res.failure == "denied by the human"
        assert exec_in(name, "test", "-d", ROOT).returncode == 0  # the first mkdir ran
        assert exec_in(name, "test", "-e", f"{ROOT}/models").returncode != 0  # the second never did


def test_a_denied_first_approval_leaves_no_partial_state(store, release):
    with exec_box() as name:
        p, _, res = deploy_and_verify(store, release, name, answers=["deny"])
        assert not res.ok and UndoLog(store, "box-1").pending() == []
        assert exec_in(name, "test", "-e", ROOT).returncode != 0


def test_a_denial_midway_is_fully_covered_by_the_undo_log(store, release):
    with exec_box() as name:
        p, _, res = deploy_and_verify(store, release, name, answers=["approve"] * 3 + ["deny"])
        assert not res.ok and res.needs_undo and UndoLog(store, "box-1").pending()
        assert exec_in(name, "test", "-e", ROOT).returncode == 0  # some state exists...
        ok, msg = provisioner_undo(p)
        assert ok, msg
        assert exec_in(name, "test", "-e", ROOT).returncode != 0  # ...and one approved command removes it all
        assert UndoLog(store, "box-1").pending() == []


def provisioner_undo(p):
    p.gate.ask = lambda prompt: "approve"
    return p.undo("box-1", Transport.docker_exec)


def test_a_denied_undo_changes_nothing(store, release):
    with exec_box() as name:
        p, _, res = deploy_and_verify(store, release, name)
        p.gate.ask = lambda prompt: "no"
        ok, msg = p.undo("box-1", Transport.docker_exec)
        assert not ok and "unchanged" in msg and exec_in(name, "test", "-e", ROOT).returncode == 0


def test_a_read_only_filesystem_is_a_clean_honest_failure(store, release):
    with exec_box(extra=("--read-only", "--tmpfs", "/tmp")) as name:
        _, _, res = deploy_and_verify(store, release, name)
        assert not res.ok and res.steps[-1].status == "failed"
        assert "Read-only file system" in res.failure and res.model_name == "stub-1.5b"
        assert not any(s.recipe == "start_server" for s in res.steps)  # no invented success past the failure


def test_an_offline_box_without_python_fails_with_a_hint_and_installs_nothing(store, release):
    with container("envrec-fixture-nopy", "--network", "none", "-m", "2g") as name:
        p, human, res = deploy_and_verify(store, release, name)
        assert not res.ok and res.steps[-1].recipe == "install_runtime" and "python3 is missing" in res.failure
        assert "installs no system packages" in res.failure
        assert p.gate.asked == 0 and exec_in(name, "test", "-e", ROOT).returncode != 0  # nothing was even proposed


def test_a_truncated_transfer_is_caught_by_the_checksum_and_fully_undone(store, release):
    from envrec.remote.runner import _real_exec
    worker_len = len((release.dir / release.filename).read_bytes())

    def cut_the_transfer(argv, stdin, timeout):
        if stdin is not None and len(stdin) == worker_len:
            stdin = stdin[: worker_len // 2]  # the link dies half way
        return _real_exec(argv, stdin, timeout)

    with exec_box() as name:
        p, _, res = deploy_and_verify(store, release, name, exec_fn=cut_the_transfer)
        assert not res.ok and "checksum mismatch on the device" in res.failure and res.needs_undo
        assert not any(s.recipe == "start_server" for s in res.steps)
        ok, msg = provisioner_undo(p)
        assert ok and exec_in(name, "test", "-e", ROOT).returncode != 0


def test_a_killed_transfer_process_is_a_recorded_failure_not_a_success(store, release):
    from envrec.remote.runner import _real_exec

    def killed(argv, stdin, timeout):
        return (137, b"", b"killed") if stdin is not None and any("dd" == a for a in argv) else _real_exec(argv, stdin, timeout)

    with exec_box() as name:
        p, _, res = deploy_and_verify(store, release, name, exec_fn=killed)
        assert not res.ok and "transfer failed" in res.failure
        assert provisioner_undo(p)[0] and exec_in(name, "test", "-e", ROOT).returncode != 0


def test_lying_ram_the_stricter_cgroup_limit_is_used(store, release):
    with exec_box("2g") as name:
        p, _, res = deploy_and_verify(store, release, name)
        pf = res.preflight
        assert pf.ram_reported_gib > 5 and pf.ram_limit_gib == 2.0 and pf.ram_effective_gib == 2.0
        from envrec.remote.sizing import ram_cap_params_b
        assert ram_cap_params_b(pf.ram_reported_gib) > ram_cap_params_b(pf.ram_effective_gib)  # the lie would have allowed more
        assert res.model_name == "stub-1.5b" and res.model.limited_by in ("ram", "none")
        facts = {f.key: f.value for f in store.current_facts(p.targets["box-1"].id)}
        assert facts["hw.ram_total_gib"] == 2.0 and facts["hw.ram_host_reported_gib"] == pf.ram_reported_gib
        provisioner_undo(p)


def test_a_tampered_artifact_aborts_before_any_approval_or_connection(store, release, tmp_path):
    import shutil
    bad = tmp_path / "dist"
    shutil.copytree(release.dir, bad)
    (bad / release.filename).write_bytes((bad / release.filename).read_bytes() + b"tamper")
    bad_release = Release(release.version, release.filename, release.sha256, release.models, bad)
    with exec_box() as name:
        p, human = provisioner(store, bad_release, exec_entry(name), APPROVE)
        with pytest.raises(ChecksumMismatch):
            p.deploy("box-1", Transport.docker_exec)
        assert p.gate.asked == 0 and p.runner.attempts == [] and exec_in(name, "test", "-e", ROOT).returncode != 0


def test_the_model_ceiling_in_targets_yaml_caps_the_choice(store, release):
    with exec_box("4g") as name:
        small = exec_entry(name, limits=EntryLimits(max_disk_gb=20, max_model_params="1B"))
        p, _ = provisioner(store, release, small, APPROVE)
        res = p.deploy("box-1", Transport.docker_exec)
        assert res.ok and res.model_name == "stub-1b" and res.model.limited_by == "config_params"
        provisioner_undo(p)

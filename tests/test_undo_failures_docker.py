"""R5 on real containers: interrupted runs are resumable or undoable, a killed server is detected, and undo frees the space."""

from __future__ import annotations

import json
import re
import time

import pytest

from docker_helpers import exec_in, need_docker
from envrec.remote.approval import UndoLog
from envrec.remote.provision import ROOT, SERVER_PORT
from envrec.mission import Transport
from envrec.verify import ExpectedModel
from runtime_fixtures import fake_plan
from test_provision_docker import (APPROVE, DEPLOY_WRITES, Answers, deploy_it, exec_box, exec_entry, provisioner, release, store,  # noqa: F401
                                   undo)

pytestmark = need_docker("envrec-fixture-sshd", "envrec-fixture-nopy")


class Interrupted(Exception):
    """Stands in for ctrl-C / a dead controller: raised out of the approval prompt on the Nth question."""


def interrupting(after: int):
    state = {"n": 0}

    def ask(prompt):
        state["n"] += 1
        if state["n"] > after:
            raise Interrupted()
        return "approve"
    return ask


@pytest.mark.parametrize("after", [2, 5, 8, 9])
def test_an_interrupted_deploy_can_be_undone_completely(store, release, tmp_path, after):  # noqa: F811
    plan = fake_plan(tmp_path / "art", model_size=2 * 1024 * 1024)
    with exec_box() as name:
        p, _ = provisioner(store, release, exec_entry(name), [])
        p.gate.ask = interrupting(after)
        with pytest.raises(Interrupted):
            p.deploy("box-1", Transport.docker_exec, plan)
        assert UndoLog(store, "box-1").pending(), "the undo entries were recorded before the writes that were interrupted"
        assert exec_in(name, "test", "-e", ROOT).returncode == 0
        ok, msg = undo(p)
        assert ok, msg
        assert exec_in(name, "test", "-e", ROOT).returncode != 0 and UndoLog(store, "box-1").pending() == []
        assert exec_in(name, "pgrep", "-f", "llama-server").returncode != 0


def test_an_interrupted_deploy_can_be_resumed_by_running_it_again(store, release, tmp_path):  # noqa: F811
    plan = fake_plan(tmp_path / "art", model_size=2 * 1024 * 1024)
    with exec_box() as name:
        p, _ = provisioner(store, release, exec_entry(name), [])
        p.gate.ask = interrupting(8)  # dies while the model file is in flight
        with pytest.raises(Interrupted):
            p.deploy("box-1", Transport.docker_exec, plan)
        p.gate.ask = lambda prompt: "approve"
        res = p.deploy("box-1", Transport.docker_exec, plan)  # same plan: every step is idempotent or overwrites
        assert res.ok, res.failure
        ok, msg = undo(p)
        assert ok, msg
        assert exec_in(name, "test", "-e", ROOT).returncode != 0


def test_a_server_killed_mid_run_is_detected_and_undo_still_cleans_everything(store, release, tmp_path):  # noqa: F811
    plan = fake_plan(tmp_path / "art")
    with exec_box() as name:
        p, _, res = deploy_it(store, release, plan, name)
        assert res.ok
        exec_in(name, "pkill", "-9", "-f", "llama-server")
        time.sleep(1.5)
        st = json.loads(p.runner.run("box-1", Transport.docker_exec, "runtime_status", {"worker": res.facts["worker"], "root": ROOT}).stdout)
        assert st["state"] != "running"
        v = p.verify("box-1", Transport.docker_exec, res, ExpectedModel(plan.model.id, plan.model.file))
        assert not v.ok and "generation" in v.failed
        ok, msg = undo(p)
        assert ok, msg
        assert exec_in(name, "test", "-e", ROOT).returncode != 0


def test_undo_frees_the_space_it_used_and_says_so(store, release, tmp_path):  # noqa: F811
    plan = fake_plan(tmp_path / "art", model_size=48 * 1024 * 1024)
    with exec_box() as name:
        p, _, res = deploy_it(store, release, plan, name)
        assert res.ok, res.failure
        ok, msg = undo(p)
        assert ok, msg
        freed = int(re.search(r"changed by \+(\d+) MB", msg).group(1)) if re.search(r"changed by \+(\d+) MB", msg) else 0
        assert freed >= 40, msg  # the 48 MB model alone was installed
        assert "port 8089 closed" in msg


def test_a_real_model_verifier_run_through_the_provisioner_against_a_container(store, release, tmp_path):  # noqa: F811
    plan = fake_plan(tmp_path / "art")
    with exec_box() as name:
        p, _, res = deploy_it(store, release, plan, name)
        assert res.ok, res.failure
        v = p.verify("box-1", Transport.docker_exec, res, ExpectedModel(plan.model.id, plan.model.file))
        assert v.ok, v.reason
        assert v.rss_mib and v.rss_mib > 0 and v.tokens_per_s and v.startup_s
        facts = {f.key: f for f in store.current_facts(p.targets["box-1"].id)}
        assert facts["host.model_server"].status.value == "verified" and facts["host.model_rss_mib"].value == v.rss_mib
        assert undo(p)[0]


def test_a_server_that_listens_beyond_loopback_fails_bind_in_a_real_container(store, release, tmp_path):  # noqa: F811
    from runtime_fixtures import FAKES, artifact, make_runtime_archive
    import dataclasses
    plan = fake_plan(tmp_path / "art")
    script = tmp_path / "bind_all.py"
    script.write_text((FAKES / "fake_llama_server.py").read_text().replace('os.environ.get("FAKE_MODE", "ok")', '"bind_all"'))
    arc = make_runtime_archive(tmp_path / "bad", script, name="bind.tar.gz")
    bad = dataclasses.replace(plan, runtime=artifact(arc, "runtime", "bind-runtime"), runtime_path=arc)
    with exec_box() as name:
        p, _, res = deploy_it(store, release, bad, name)
        assert res.ok, res.failure
        v = p.verify("box-1", Transport.docker_exec, res, ExpectedModel(bad.model.id, bad.model.file))
        assert not v.ok and v.failed == ["bind_loopback"], (v.failed, v.details)
        assert p.store.current_fact(p.targets["box-1"].id, "host.model_server") is None
        assert undo(p)[0]


def test_a_missing_system_library_is_named_with_a_hint_and_undone(store, release, tmp_path):  # noqa: F811
    import dataclasses
    from runtime_fixtures import artifact, make_runtime_archive
    plan = fake_plan(tmp_path / "art")
    script = tmp_path / "needs_lib.py"
    script.write_text("#!/usr/bin/env python3\nimport sys\nprint('%s: error while loading shared libraries: libfoo.so.9: cannot open shared object file', sys.argv[0], file=sys.stderr)\nsys.exit(127)\n")
    arc = make_runtime_archive(tmp_path / "bad", script, name="lib.tar.gz")
    bad = dataclasses.replace(plan, runtime=artifact(arc, "runtime", "lib-runtime"), runtime_path=arc)
    with exec_box() as name:
        p, _, res = deploy_it(store, release, bad, name)
        assert not res.ok and "missing the system library libfoo.so.9" in res.failure and "install it yourself" in res.failure
        assert undo(p)[0] and exec_in(name, "test", "-e", ROOT).returncode != 0

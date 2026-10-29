"""P9: end-to-end scenarios on real containers of different RAM, OS and CPU architecture (Docker only).

The other P9 cases live in tests/test_provision_docker.py: sabotage (killed and truncated transfers, with full
undo) and lying RAM (cgroup limit stricter than the host's number).
"""

import contextlib
from pathlib import Path

import pytest
import yaml

from docker_helpers import container, exec_in, need_docker
from envrec.mission import DockerExecBlock, EntryLimits, TargetConfig, TargetEntry, Transport
from envrec.remote.approval import ApprovalGate
from envrec.remote.fleet import inventory_fleet, render_table
from envrec.remote.provision import ROOT, SERVER_PORT, DeployPlan, Provisioner, Release
from envrec.runtime import Fetcher
from envrec.remote.runner import RemoteRunner
from envrec.remote.sizing import choose_model
from envrec.scoring import GroundTruth, GTField, score_target
from envrec.store import Store
from envrec.verify import ExecClient, verify_server
from runtime_fixtures import fake_plan

ROOT_DIR = Path(__file__).resolve().parent.parent
FLEET = yaml.safe_load((ROOT_DIR / "ground_truth" / "fleet.yaml").read_text())["devices"]
pytestmark = need_docker("envrec-fixture-sshd", "python:3.12-alpine", "debian:bookworm-slim")


@pytest.fixture(scope="module")
def release(tmp_path_factory):
    import sys
    sys.path.insert(0, str(ROOT_DIR / "build" / "worker"))
    import build as worker_build
    out = tmp_path_factory.mktemp("dist")
    worker_build.build(out)
    return Release.load(ROOT_DIR / "build" / "worker" / "release.json", out)


@pytest.fixture(scope="module")
def plan(tmp_path_factory):
    return fake_plan(tmp_path_factory.mktemp("art"), threads=2, context=512, max_rss_mib=800, min_free_disk_mb=200)


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "s") as s:
        yield s


@contextlib.contextmanager
def fleet(ids):
    with contextlib.ExitStack() as stack:
        names = {}
        for i in ids:
            spec = FLEET[i]
            names[i] = stack.enter_context(container(spec["image"], "--platform", spec["platform"], "-m", spec["memory"],
                                                      "--entrypoint", "sleep", command=["infinity"]))
        yield names


def make(store, release, names):
    entries = [TargetEntry(id=i, name=i, host="127.0.0.1", allowed_transports=[Transport.docker_exec],
                           docker_exec=DockerExecBlock(container=n), limits=EntryLimits(max_disk_gb=20, max_model_params="13B"),
                           owner_confirmed=True) for i, n in names.items()]
    cfg = TargetConfig(targets=entries)
    asked = []
    gate = ApprovalGate(store, lambda prompt: (asked.append(prompt), "approve")[1], lambda text: None)
    return Provisioner(RemoteRunner(cfg), gate, store, cfg, release), asked


def truth_for(i):
    return GroundTruth(machine=i, confirmed=True, facts={k: GTField(value=v, confirmed=True, source="how the container was started")
                                                         for k, v in FLEET[i]["facts"].items()})


def test_the_ground_truth_is_consistent_with_the_size_table_on_its_own():
    """Independent of any probe: the hand-written expected model matches the table for the RAM the container was given."""
    for i, spec in FLEET.items():
        ram = float(spec["memory"].rstrip("g"))
        assert spec["facts"]["hw.ram_total_gib"] == ram
        assert f"stub-{choose_model(ram, 100, 100, 100).params_b:g}b" == spec["model"], i


def test_fleet_inventory_across_five_boxes_scores_against_ground_truth(store, release):
    ids = list(FLEET)
    with fleet(ids) as names:
        p, asked = make(store, release, names)
        rows = inventory_fleet(p, {i: Transport.docker_exec for i in ids})
        print("\n" + render_table(rows))
        assert asked == []  # a whole-fleet inventory needs no approval: it is read-only
        assert all(r.ok for r in rows), [r.note for r in rows if not r.ok]
        for r in rows:
            report = score_target(store, p.targets[r.target_id].id, truth_for(r.target_id))
            assert {x.outcome for x in report.rows} == {"correct"}, (r.target_id, [(x.key, x.expected, x.actual) for x in report.rows])
            assert not report.provisional
        by = {r.target_id: r for r in rows}
        assert {by[i].preflight.arch for i in ids} == {"aarch64", "x86_64"}
        assert by["alpine-1"].preflight.os_id == "alpine" and by["lap-a"].preflight.os_id == "debian"
        assert [bool(by[i].preflight.python) for i in ("lap-a", "alpine-1", "x86-bare")] == [True, True, False]


def test_fleet_assignment_gives_each_box_the_size_the_table_says(store, release):
    ids = list(FLEET)
    with fleet(ids) as names:
        p, _ = make(store, release, names)
        rows = {r.target_id: r for r in inventory_fleet(p, {i: Transport.docker_exec for i in ids})}
        for i in ids:
            assert f"stub-{rows[i].model_params_b:g}b" == FLEET[i]["model"], i
        assert rows["lap-a"].model_params_b > rows["alpine-1"].model_params_b > rows["small-1"].model_params_b


def test_laptop_to_laptop_then_a_different_os(store, release, plan):
    ids = ["lap-a", "lap-b", "alpine-1"]
    with fleet(ids) as names:
        p, asked = make(store, release, names)
        results = {}
        for i in ids:
            before = len(asked)
            res = p.deploy(i, Transport.docker_exec, plan)
            assert res.ok, (i, res.failure)
            results[i] = res
            assert len(asked) - before == 10  # every device gets its own fresh set of approvals
            v = verify_server(ExecClient(p.runner, i, Transport.docker_exec, SERVER_PORT), store, p.targets[i])
            assert v.ok, (i, v.reason)
            assert exec_in(names[i], "python3", "-c",
                           "import re;t=open('/proc/net/tcp').read();print('LOCAL' if '0100007F:1F99' in t and '00000000:1F99' not in t else 'BAD')"
                           ).stdout.strip() == "LOCAL"
        # same OS, same profile: same outcome; a different OS (musl, busybox) also works with the same templates and runtime
        assert results["lap-a"].model_name == results["lap-b"].model_name == plan.model.id
        assert results["lap-a"].preflight.os_id == results["lap-b"].preflight.os_id == "debian"
        assert results["alpine-1"].preflight.os_id == "alpine"
        for i in ids:
            ok, msg = p.undo(i, Transport.docker_exec)
            assert ok, (i, msg)
            assert exec_in(names[i], "test", "-e", ROOT).returncode != 0


def test_a_box_of_another_cpu_architecture_without_python_fails_cleanly_with_a_hint(store, release, plan):
    with fleet(["x86-bare"]) as names:
        p, asked = make(store, release, names)
        res = p.deploy("x86-bare", Transport.docker_exec, plan)
        assert not res.ok and res.preflight.arch == "x86_64" and "python3 is missing" in res.failure
        assert asked == [] and exec_in(names["x86-bare"], "test", "-e", ROOT).returncode != 0


def test_one_box_going_down_does_not_hide_the_rest_of_the_inventory(store, release):
    with fleet(["lap-a", "small-1"]) as names:
        names["ghost"] = "no-such-container-xyz"
        p, _ = make(store, release, names)
        rows = inventory_fleet(p, {i: Transport.docker_exec for i in names})
        assert [r.ok for r in rows] == [True, True, False] and "PreflightError" in rows[2].note


def test_one_download_is_pushed_to_several_devices_from_the_controllers_cache(store, release, tmp_path):
    """R2: nothing is re-downloaded per device; an air-gapped box gets its files from the controller only."""
    import dataclasses
    from test_fetch import Origin, art
    from runtime_fixtures import make_model, make_runtime_archive
    arc, mod = make_runtime_archive(tmp_path / "src"), make_model(tmp_path / "src")
    origin = Origin({"/rt.tar.gz": arc.read_bytes(), "/model.gguf": mod.read_bytes()})
    rt_art, md_art = art(origin, "/rt.tar.gz", arc.read_bytes(), id="rt"), art(origin, "/model.gguf", mod.read_bytes(), id="md")
    with fleet(["lap-a", "small-1"]) as names:
        p, asked = make(store, release, names)
        fetcher = Fetcher({"rt": rt_art, "md": md_art}, tmp_path / "cache", p.gate, reserve_gib=0.0001)
        plan = DeployPlan(rt_art, fetcher.fetch("rt"), md_art, fetcher.fetch("md"), kind="llama.cpp", binary_name="llama-server", threads=1, context=256, max_rss_mib=500, min_free_disk_mb=100)
        for i in ("lap-a", "small-1"):
            res = p.deploy(i, Transport.docker_exec, plan)
            assert res.ok, (i, res.failure)
        assert origin.hits == ["/rt.tar.gz", "/model.gguf"]  # each artifact crossed the network once, for two devices
        for i in ("lap-a", "small-1"):
            assert p.undo(i, Transport.docker_exec)[0]
    origin.stop()

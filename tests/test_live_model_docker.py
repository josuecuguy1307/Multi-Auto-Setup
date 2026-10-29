"""R6: the real runtime and a real small model, provisioned into real containers, verified, then removed.

Opt-in: `pytest --live-model tests/test_live_model_docker.py`. Skipped by default. It downloads the pinned llama.cpp build
(about 20 MB) and the smallest smoke-set model (about 0.5 GB) from the sources in config/runtime_manifest.yaml into
`.live-cache/` (reused on later runs), refuses to leave less than the manifest's disk reserve free, and needs
`approved: true` in that manifest. Passing `--live-model` is the human's go-ahead for exactly the smoke-set artifacts of the
chosen tier (`--live-tier`); anything else the Fetcher is asked for is denied.

Containers: the Debian fixture on this machine's own architecture at two RAM sizes, and the other architecture (emulated) when
an image for it exists: `docker build --platform linux/amd64 -t envrec-fixture-sshd-amd64 -f tests/docker/Dockerfile.sshd tests/docker`.
Each run writes its numbers to `live_results/r6-*.json`.
"""

from __future__ import annotations

import json
import platform
import shutil
import sys
import time
from pathlib import Path

import pytest

from docker_helpers import container, docker_ready, exec_in
from envrec.mission import DockerExecBlock, EntryLimits, TargetConfig, TargetEntry, Transport
from envrec.remote.approval import ApprovalGate
from envrec.remote.provision import ROOT, SERVER_PORT, DeployPlan, Provisioner, Release
from envrec.remote.runner import RemoteRunner
from envrec.runtime import Fetcher, Manifest
from envrec.store import Store
from envrec.verify import ExpectedModel

REPO = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.live_model
NATIVE = "arm64" if platform.machine() in ("arm64", "aarch64") else "amd64"
OTHER = "amd64" if NATIVE == "arm64" else "arm64"
IMAGES = {NATIVE: "envrec-fixture-sshd", OTHER: f"envrec-fixture-sshd-{OTHER}"}


@pytest.fixture(scope="module")
def manifest():
    m = Manifest.load(REPO / "config" / "runtime_manifest.yaml")
    if not m.approved:
        pytest.skip("config/runtime_manifest.yaml says approved: false: nothing is downloaded until the human sets it to true")
    return m


@pytest.fixture(scope="module")
def tier(request):
    return request.config.getoption("--live-tier")


@pytest.fixture(scope="module")
def release(tmp_path_factory):
    sys.path.insert(0, str(REPO / "build" / "worker"))
    import build as worker_build
    out = tmp_path_factory.mktemp("dist")
    worker_build.build(out)
    return Release.load(REPO / "build" / "worker" / "release.json", out)


@pytest.fixture(scope="module")
def fetcher(manifest, tier, tmp_path_factory):
    wanted = {a for a in manifest.smoke_artifacts() if a == tier or a.startswith("llama.cpp:")}
    store = Store(tmp_path_factory.mktemp("fetch-store"))
    asked, shown = [], []
    shas = [manifest.smoke_artifacts()[a].sha256 for a in wanted]

    def ask(prompt):
        # `--live-model` approves exactly the smoke-set artifacts of the chosen tier and the pinned runtime builds
        ok = bool(shown) and any(sha in shown[-1] for sha in shas)
        asked.append((shown[-1] if shown else "", ok))
        return "approve" if ok else "deny"

    f = Fetcher.from_manifest(manifest, REPO / ".live-cache", ApprovalGate(store, ask, shown.append))
    f.asked_prompts = asked
    yield f
    store.close()


def plan_for(manifest, fetcher, tier, arch, mem_gib) -> DeployPlan:
    arts = manifest.smoke_artifacts()
    rt = next(a for a in arts.values() if a.kind == "runtime" and a.meta["platform"] == f"linux/{arch}")
    md = arts[tier]
    src = next(s for s in manifest.raw["runtime_sources"] if s["id"] == rt.source)
    return DeployPlan(runtime=rt, runtime_path=fetcher.fetch(rt.id), model=md, model_path=fetcher.fetch(md.id), kind=rt.source,
                      binary_name=src["server_binary"], threads=2, context=md.meta.get("context") or 2048,
                      max_rss_mib=int(mem_gib * 1024 * 0.85), min_free_disk_mb=300)


def record(name: str, data: dict) -> None:
    out = REPO / "live_results"
    out.mkdir(exist_ok=True)
    (out / f"r6-{name}-{int(time.time())}.json").write_text(json.dumps(data, indent=2, default=str))


CASES = [(NATIVE, "1g", 1), (NATIVE, "2g", 2), (OTHER, "2g", 2)]


@pytest.mark.parametrize(("arch", "mem", "mem_gib"), CASES, ids=[f"{a}-{m}" for a, m, _ in CASES])
def test_real_runtime_and_model_are_provisioned_verified_and_removed(arch, mem, mem_gib, manifest, fetcher, tier, release, tmp_path):
    image = IMAGES[arch]
    if not docker_ready(image):
        pytest.skip(f"no fixture image {image} for {arch}")
    plan = plan_for(manifest, fetcher, tier, arch, mem_gib)
    free_before = shutil.disk_usage(REPO).free
    extra = ["--platform", f"linux/{arch}"] if arch != NATIVE else []
    with container(image, "-m", mem, "--entrypoint", "sleep", *extra, command=["infinity"]) as name:
        entry = TargetEntry(id="live-1", name="Live box", host="127.0.0.1", allowed_transports=[Transport.docker_exec],
                            docker_exec=DockerExecBlock(container=name), limits=EntryLimits(max_disk_gb=20, max_model_params="8B"),
                            owner_confirmed=True)
        cfg = TargetConfig(targets=[entry])
        with Store(tmp_path / "s") as store:
            gate = ApprovalGate(store, lambda p: "approve", lambda t: None)  # the scripted human of every docker test
            prov = Provisioner(RemoteRunner(cfg), gate, store, cfg, release)
            data: dict = {"arch": arch, "container_ram": mem, "tier": tier, "runtime": plan.runtime.id, "model": plan.model.id,
                          "bytes_cached": fetcher.total_cached_bytes(), "free_disk_before": free_before}
            try:
                res = prov.deploy("live-1", Transport.docker_exec, plan)
                data.update(deploy_ok=res.ok, failure=res.failure, steps=[(s.recipe, s.status, s.detail) for s in res.steps])
                if not res.ok:  # the first run of a real runtime is where wrong assumptions show; say exactly which
                    tail = exec_in(name, "tail", "-n", "20", f"{ROOT}/server.log").stdout
                    data["server_log_tail"] = tail
                assert res.ok, f"{res.failure}\n{data.get('server_log_tail', '')}"
                v = prov.verify("live-1", Transport.docker_exec, res, ExpectedModel(plan.model.id, plan.model.file))
                data.update(verified=v.ok, failed=v.failed, details=v.details, tokens_per_s=v.tokens_per_s, rss_mib=v.rss_mib,
                            startup_s=v.startup_s, tokens_per_s_source=v.tokens_per_s_source, sample_replies=v.replies)
                assert v.ok, v.reason
                assert v.rss_mib and v.rss_mib <= plan.max_rss_mib  # the cap held
                ok, msg = prov.undo("live-1", Transport.docker_exec)
                data.update(undo_ok=ok, undo_message=msg)
                assert ok, msg
                assert exec_in(name, "test", "-e", ROOT).returncode != 0
            finally:
                data["free_disk_after_controller"] = shutil.disk_usage(REPO).free
                record(f"{arch}-{mem}", data)

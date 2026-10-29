"""R3: rule-table baseline, closed-menu refinement, clean refusal, unfamiliar hardware -> written proposal only."""

from __future__ import annotations

import asyncio

import pytest

from envrec.catalog import Catalog
from envrec.catalog.model import Lock, ModelFile, RuntimeAsset
from envrec.model.scripted import ScriptedModel
from envrec.pairing import DeviceProfile, choose_pair, feasible, write_proposal
from envrec.remote.preflight import Preflight
from envrec.store import Store

H = "a" * 64


def rt(arch, backend="cpu", os="linux", runnable=True, source="rtA"):
    return RuntimeAsset(id=f"{source}:t1:{os}/{arch}:{backend}", source=source, tag="t1", os=os, arch=arch, backend=backend, file="f.tgz",
                        url="https://x/f", sha256=H, bytes=20_000_000, license="MIT", license_source="u", formats=["gguf"],
                        server_binary="srv", runnable=runnable)


def md(params, quant="Q4_K_M", min_ram=None):
    size = int(params * 0.6e9 + 0.1e9)
    return ModelFile(id=f"pub/m{params:g}b/m-{quant}.gguf", source="pub", repo=f"pub/m{params:g}b", file=f"m-{quant}.gguf", url="https://x/m",
                     revision="r" * 40, sha256=H, bytes=size, license="apache-2.0", license_source="u", format="gguf", quantization=quant,
                     params_b=params, min_ram_gib=min_ram if min_ram is not None else round(size / 2**30 * 1.25 + 0.3, 2))


def catalog(runtimes=None, models=None):
    runtimes = runtimes or [rt("arm64"), rt("amd64"), rt("amd64", "cuda")]
    models = models or [md(0.5), md(1.5), md(3), md(4), md(7), md(1.5, "Q8_0")]
    pairs = [(r.id, m.id) for r in runtimes for m in models]
    return Catalog(Lock(manifest_sha256="x", runtime_assets=runtimes, model_files=models, pairs=pairs))


def prof(ram, arch="arm64", disk=50.0, accel="none", **kw):
    return DeviceProfile("dev", "linux", arch, ram, disk, accel, 4, **kw)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "s") as s:
        yield s


@pytest.mark.parametrize("ram,params", [(1.0, 0.5), (2.0, 1.5), (4.5, 3.0), (8.0, 7.0), (16.0, 7.0)])
def test_baseline_follows_the_ram_table(ram, params, store):
    res = run(choose_pair(catalog(), prof(ram), store=store))
    assert res.entry.model.params_b == params and res.source == "baseline" and res.entry.runtime.backend == "cpu"
    assert res.entry.model.quantization == "Q4_K_M"


def test_stricter_limit_wins_target_params(store):
    assert run(choose_pair(catalog(), prof(16, max_params_b=1.0), store=store)).entry.model.params_b == 0.5


def test_stricter_limit_wins_disk(store):
    res = run(choose_pair(catalog(), prof(16, disk=50, max_disk_gb=0.9), store=store))
    assert res.entry.model.params_b == 0.5  # 0.4 GB model + 0.02 GB runtime, x1.1 fits 0.9; 1.5B does not


def test_ram_floor_of_the_entry_is_respected(store):
    cat = catalog(models=[md(0.5, min_ram=6.0), md(1.5, min_ram=1.0)])
    res = run(choose_pair(cat, prof(2.0), store=store))
    assert res.entry.model.params_b == 1.5  # the 0.5B entry declares it needs 6 GiB, so it is not feasible


def test_nothing_fits_refuses_cleanly_and_says_why(store):
    res = run(choose_pair(catalog(), prof(0.5), store=store))
    assert res.entry is None and res.source == "refused" and "nothing in the catalog fits" in res.reason and not res.needs_proposal
    ev = store.events(type="pair_refused")
    assert len(ev) == 1 and ev[0].payload["rejected"]


def test_platform_filter_and_unsupported_runtimes(store):
    cat = catalog([rt("amd64", runnable=False)])
    res = run(choose_pair(cat, prof(8, arch="amd64"), store=store))
    assert res.entry is None and "runtime_not_supported_by_worker" in str(res.rejected)


def test_gpu_backends_need_the_hardware_but_baseline_stays_cpu(store):
    cat = catalog()
    assert all(e.runtime.backend == "cpu" for e in feasible(cat, prof(8, arch="amd64")).entries)
    with_gpu = feasible(cat, prof(8, arch="amd64", accel="nvidia"))
    assert any(e.runtime.backend == "cuda" for e in with_gpu.entries)
    assert run(choose_pair(cat, prof(8, arch="amd64", accel="nvidia"), store=store)).entry.runtime.backend == "cpu"


# ---- the model's part -------------------------------------------------------------------------------------------

def test_model_may_pick_a_smaller_menu_entry(store):
    cat, p = catalog(), prof(8)
    m = ScriptedModel(['{"choice": "3", "why": "headroom"}'])
    res = run(choose_pair(cat, p, store=store, model=m))
    assert res.source == "model" and res.entry.id == res.menu["3"] and res.entry.id != res.baseline_id
    ev = store.events(type="pair_choice")[0].payload
    assert ev["source"] == "model" and ev["baseline"] == res.baseline_id and ev["chosen"] == res.entry.id and ev["menu"]


def test_menu_contains_only_entries_that_fit(store):
    m = ScriptedModel(['{"choice": "1"}'])
    run(choose_pair(catalog(), prof(2.0), store=store, model=m))
    prompt = m.calls[0][1]
    assert "7B" not in prompt and "4B" not in prompt and "1.5B" in prompt


@pytest.mark.parametrize("bad", ["I think the big one", '{"choice": "99"}', '{"choice": "qwen-7b"}', "{not json", '{"model": "x"}'])
def test_invalid_replies_fall_back_to_the_baseline(bad, store):
    m = ScriptedModel([bad, bad])
    res = run(choose_pair(catalog(), prof(8), store=store, model=m))
    assert res.source == "fallback_invalid" and res.entry.id == res.baseline_id and len(res.errors) == 2 and len(m.calls) == 2


def test_second_attempt_can_succeed_and_carries_the_error(store):
    m = ScriptedModel(["nonsense", '{"choice": "2"}'])
    res = run(choose_pair(catalog(), prof(8), store=store, model=m))
    assert res.source == "model" and "rejected" in m.calls[1][1]


def test_backend_error_falls_back(store):
    m = ScriptedModel([RuntimeError("down"), RuntimeError("down")])
    res = run(choose_pair(catalog(), prof(8), store=store, model=m))
    assert res.source == "fallback_invalid" and res.entry.id == res.baseline_id


def test_a_model_cannot_name_a_runtime_outside_the_catalog(store):
    m = ScriptedModel(['{"choice": "someother-runtime:v9:linux/arm64:cpu"}'] * 2)
    res = run(choose_pair(catalog(), prof(8), store=store, model=m))
    assert res.source == "fallback_invalid" and "someother" not in res.entry.id


def test_no_model_call_when_only_one_option(store):
    m = ScriptedModel([])
    res = run(choose_pair(catalog(models=[md(0.5)], runtimes=[rt("arm64")]), prof(8), store=store, model=m))
    assert res.source == "baseline" and m.calls == []


# ---- unfamiliar hardware / runtime outside the manifest -> proposal, nothing else -------------------------------------

def test_unfamiliar_hardware_refuses_and_a_proposal_is_written(store, tmp_path):
    p = DeviceProfile("rv", "linux", "riscv64", 8, 50, "none", 4)
    m = ScriptedModel([])
    res = run(choose_pair(catalog(), p, store=store, model=m))
    assert res.entry is None and res.needs_proposal and "unfamiliar" in res.reason and m.calls == []
    asked = []
    path = write_proposal(p, catalog(), res.reason, tmp_path / "proposals", research=lambda q: asked.append(q) or "docs say: riscv64 builds exist upstream")
    text = path.read_text()
    assert "Nothing was downloaded" in text and "riscv64" in text and "docs say" in text and asked
    assert path.parent == tmp_path / "proposals" and list(path.parent.iterdir()) == [path]


def test_unknown_accelerator_is_unfamiliar(store):
    res = run(choose_pair(catalog(), prof(8, accel="npu-x"), store=store))
    assert res.needs_proposal and "accelerator" in res.reason


def test_runtime_not_in_catalog_is_never_chosen_even_if_requested(store):
    res = run(choose_pair(catalog(), prof(8), store=store, requested_runtime="shiny-new-runtime"))
    assert res.entry is None and res.needs_proposal and "never fetched" in res.reason
    assert store.events(type="pair_refused")[0].payload["chosen"] is None


def test_requested_runtime_that_is_in_catalog_is_fine(store):
    assert run(choose_pair(catalog(), prof(8), store=store, requested_runtime="rtA")).entry is not None


def test_proposal_without_research_says_so(tmp_path):
    p = DeviceProfile("rv", "linux", "riscv64", 8, 50, "none", 4)
    assert "no research function" in write_proposal(p, catalog(), "x", tmp_path).read_text()


def test_profile_from_preflight_and_signature():
    pf = Preflight(arch="aarch64", os_name="Linux", os_id="debian", ram_reported_gib=7.6, ram_limit_gib=2.0, disk_free_gb=30, gpu=False,
                   pkg_manager="apt-get", python="3.11", cpus=4, accel="none")
    p = DeviceProfile.from_preflight("x", pf)
    assert (p.os, p.arch, p.ram_gib) == ("linux", "arm64", 2.0)
    assert p.signature == {"arch": "arm64", "os": "linux", "ram_bracket": "2-4", "accelerator": "none"}
    assert DeviceProfile("a", "linux", "arm64", 3.9, 1, "none").signature == p.signature  # same bracket, same signature
    assert DeviceProfile("a", "linux", "arm64", 4.0, 1, "none").signature != p.signature

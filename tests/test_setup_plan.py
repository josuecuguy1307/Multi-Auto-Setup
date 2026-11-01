"""The plan builder and the recipe: one fixture per cell of the build matrix (a supported cell gives a plan with the right build, commands and server arguments for that operating system and backend; an
unsupported cell refuses with a typed reason). SIMULATED-LOCAL for the cells this machine is not; the Windows CPU cell and this Mac's Metal cell are also run on REAL captures (below)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from envrec_coordinator import options as O
from envrec_coordinator.setup_plan import PlanRefused, build_plan, load_recipes
from envrec_net import setup as P
from envrec_net import task as T
from envrec_net.identity import create
from envrec_node import setup as node_setup

RULES = O.load_rules()
CAT = O.load_catalog()
RECIPES = load_recipes()
ROOT = Path(__file__).resolve().parent.parent
OS_NAME = {"macos": "macOS", "linux": "Ubuntu 24.04", "windows": "Windows 11"}
ARCH_NAME = {"amd64": "x86_64", "arm64": "arm64"}


def machine_facts(os_, arch, path, vendor=None, *, ram_total=32.0, ram_free=24.0, vram=None):
    gpus = []
    if path == "unified":
        gpus = [{"vendor": vendor or "Apple", "name": "chip", "vram_gib": None, "memory": "unified"}]
    elif path == "gpu":
        gpus = [{"vendor": vendor, "name": "card", "vram_gib": vram or 16.0, "memory": "dedicated"}]
    elif vendor:
        gpus = [{"vendor": vendor, "name": "integrated", "vram_gib": 1.0, "memory": "shared"}]
    return {"machine.os.name": OS_NAME[os_], "machine.os.arch": ARCH_NAME[arch], "machine.model_path": path, "machine.vram_gib": (vram or 16.0) if path == "gpu" else None, "machine.gpus": gpus,
            "machine.ram_total_gib": ram_total, "machine.ram_available_gib": ram_free, "machine.disk_free_gib": 500.0, "machine.runtimes": [], "machine.ai_clis": [], "machine.api_key_variable_names": []}


# cell id -> (facts, what the plan must show). `ngl` is the number of layers the server puts on the graphics memory; `fetch` and `unpack` are words of the command shown to the owner.
SUPPORTED = {
    "macos-arm64-metal": (machine_facts("macos", "arm64", "unified", "Apple"), {"file": "macos-arm64.tar.gz", "backend": "metal", "ngl": 999, "fetch": "curl -L", "hash": "shasum -a 256", "unpack": "tar -xzf", "binary": "llama-server "}),
    "macos-arm64-cpu": (machine_facts("macos", "arm64", "cpu"), {"file": "macos-arm64.tar.gz", "backend": "cpu", "ngl": 0, "fetch": "curl -L", "hash": "shasum -a 256", "unpack": "tar -xzf", "binary": "llama-server "}),
    "macos-amd64-cpu": (machine_facts("macos", "amd64", "cpu"), {"file": "macos-x64.tar.gz", "backend": "cpu", "ngl": 0, "fetch": "curl -L", "hash": "shasum -a 256", "unpack": "tar -xzf", "binary": "llama-server "}),
    "linux-nvidia-cuda": (machine_facts("linux", "amd64", "gpu", "NVIDIA"), {"file": "ubuntu-cuda-12.8-x64.tar.gz", "backend": "cuda", "ngl": 999, "fetch": "curl -L", "hash": "sha256sum", "unpack": "tar -xzf", "binary": "llama-server "}),
    "linux-amd-vulkan": (machine_facts("linux", "amd64", "gpu", "AMD"), {"file": "ubuntu-vulkan-x64.tar.gz", "backend": "vulkan", "ngl": 999, "fetch": "curl -L", "hash": "sha256sum", "unpack": "tar -xzf", "binary": "llama-server "}),
    "linux-intel-vulkan": (machine_facts("linux", "arm64", "gpu", "Intel"), {"file": "ubuntu-vulkan-arm64.tar.gz", "backend": "vulkan", "ngl": 999, "fetch": "curl -L", "hash": "sha256sum", "unpack": "tar -xzf", "binary": "llama-server "}),
    "linux-cpu": (machine_facts("linux", "arm64", "cpu"), {"file": "ubuntu-arm64.tar.gz", "backend": "cpu", "ngl": 0, "fetch": "curl -L", "hash": "sha256sum", "unpack": "tar -xzf", "binary": "llama-server "}),
    "windows-nvidia-vulkan": (machine_facts("windows", "amd64", "gpu", "NVIDIA"), {"file": "win-vulkan-x64.zip", "backend": "vulkan", "ngl": 999, "fetch": "Invoke-WebRequest", "hash": "Get-FileHash", "unpack": "Expand-Archive", "binary": "llama-server.exe "}),
    "windows-amd-vulkan": (machine_facts("windows", "arm64", "gpu", "AMD"), {"file": "win-vulkan-arm64.zip", "backend": "vulkan", "ngl": 999, "fetch": "Invoke-WebRequest", "hash": "Get-FileHash", "unpack": "Expand-Archive", "binary": "llama-server.exe "}),
    "windows-intel-vulkan": (machine_facts("windows", "amd64", "gpu", "Intel"), {"file": "win-vulkan-x64.zip", "backend": "vulkan", "ngl": 999, "fetch": "Invoke-WebRequest", "hash": "Get-FileHash", "unpack": "Expand-Archive", "binary": "llama-server.exe "}),
    "windows-cpu": (machine_facts("windows", "amd64", "cpu", "Intel"), {"file": "win-cpu-x64.zip", "backend": "cpu", "ngl": 0, "fetch": "Invoke-WebRequest", "hash": "Get-FileHash", "unpack": "Expand-Archive", "binary": "llama-server.exe "}),
}
REFUSED = {
    "macos-amd64-gpu": (machine_facts("macos", "amd64", "gpu", "AMD"), "unsupported_cell"),
    "unified-memory-not-apple-silicon": (machine_facts("linux", "amd64", "unified", "Apple"), "unsupported_cell"),
    "linux-other-gpu": (machine_facts("linux", "amd64", "gpu", "Moore Threads"), "unsupported_cell"),
    "windows-other-gpu": (machine_facts("windows", "amd64", "gpu", "Moore Threads"), "unsupported_cell"),
}


def first_plan(facts):
    built = O.build_options(facts, choice="local")
    assert built["options"], built["removed"][:2]
    return built["options"][0], build_plan(built["options"][0], facts, CAT, RECIPES, RULES, machine_id="m1")


def test_every_cell_of_the_matrix_has_a_fixture_here_a_plan_for_a_supported_one_a_typed_refusal_for_the_others():
    cells = [c["id"] for c in RULES.data["build_matrix"]["cells"]]
    assert sorted(set(cells) - set(SUPPORTED) - set(REFUSED)) == [] and sorted((set(SUPPORTED) | set(REFUSED)) - set(cells)) == []
    assert all(("refuse" in c) == (c["id"] in REFUSED) for c in RULES.data["build_matrix"]["cells"])


@pytest.mark.parametrize("cell", sorted(SUPPORTED))
def test_a_supported_cell_gets_the_right_build_commands_and_server_arguments_for_its_os_and_backend(cell):
    facts, want = SUPPORTED[cell]
    opt, plan = first_plan(facts)
    assert opt["build"]["cell"] == cell and opt["build"]["backend"] == (want["backend"] if cell != "macos-arm64-cpu" else "cpu") and want["file"] in opt["build"]["file"]
    ops = [s.op for s in plan.steps]
    assert ops[:2] == ["fetch_check", "unpack_archive"] and ops[-3:] == ["write_server_config", "start_server", "minimal_call"] and ops.count("fetch_check") >= 2
    by = {s.op: s for s in plan.steps}
    assert want["fetch"] in plan.steps[0].exact_command and want["hash"] in plan.steps[0].exact_command and want["file"] in plan.steps[0].exact_command
    assert want["unpack"] in by["unpack_archive"].exact_command
    start = by["start_server"]
    assert want["binary"] in start.exact_command and f"-ngl {want['ngl']}" in start.exact_command and "--host 127.0.0.1" in start.exact_command  # the server is on loopback, always
    assert start.args["ngl"] == want["ngl"] and by["write_server_config"].args["backend"] == want["backend"]
    assert plan.verification.endpoint == "127.0.0.1:8080" and plan.plan_hash == P.plan_hash(plan) and plan.fit.fits
    assert all(w.startswith("<mas>/") for s in plan.steps for w in s.writes) and all(s.undo for s in plan.steps)
    pins = {s.args["sha256"] for s in plan.steps if s.op == "fetch_check"}
    assert opt["build"]["sha256"] in pins and opt["model_sha256"] in pins  # what is downloaded is what the catalog pinned


@pytest.mark.parametrize("cell", sorted(REFUSED))
def test_an_unsupported_cell_refuses_with_a_typed_reason_and_builds_no_plan(cell):
    facts, reason = REFUSED[cell]
    built = O.build_options(facts, choice="local")
    assert built["options"] == [] and built["removed"][0]["id"] == "local-runtime" and built["removed"][0]["reason"].startswith(reason)
    fake = {"id": "x", "origin": "catalog_runtime", "model_id": CAT["model_files"][0]["id"], "build": {"cell": cell}}
    with pytest.raises(PlanRefused) as e:
        build_plan(fake, facts, CAT, RECIPES, RULES, machine_id="m1")
    assert e.value.reason == reason and e.value.text


def test_every_step_of_every_supported_cells_plan_passes_the_nodes_own_check_against_the_real_recipe():
    """The builder and the node read the same recipe: a plan the builder makes is one the node accepts (and nothing else is)."""
    coord, me = create(), create()
    for cell, (facts, _) in SUPPORTED.items():
        _, plan = first_plan(facts)
        d = plan.model_dump(mode="json")
        for s in plan.steps:
            tok = T.issue_token(coord, task_id="t", node_id=me.node_id, sid="S", tools=["setup_step"], scope=T.write_scope(plan.plan_hash, plan.recipe_id, s.n, "run", s.writes, "evt_1"))
            got_plan, got_step = node_setup.authorize(T.SetupStepParams(plan=d, step=s.n, action="run"), tok, RECIPES)
            assert (got_plan.plan_hash, got_step.op) == (plan.plan_hash, s.op), (cell, s.n)


def test_the_plan_refuses_instead_of_guessing_when_the_model_does_not_fit_the_disk_is_small_or_the_option_is_stale():
    facts = machine_facts("linux", "amd64", "cpu", ram_total=64.0, ram_free=40.0)
    opt, _ = first_plan(facts)
    with pytest.raises(PlanRefused) as e:
        build_plan(opt, {**facts, "machine.ram_available_gib": 3.0}, CAT, RECIPES, RULES, machine_id="m1")
    assert e.value.reason == "does_not_fit" and "GiB" in e.value.text
    with pytest.raises(PlanRefused) as e:
        build_plan(opt, {**facts, "machine.disk_free_gib": 1.0}, CAT, RECIPES, RULES, machine_id="m1")
    assert e.value.reason == "no_disk"
    with pytest.raises(PlanRefused) as e:  # the option was made for another kind of machine than the one it is now being planned for
        build_plan(opt, machine_facts("macos", "arm64", "unified", "Apple"), CAT, RECIPES, RULES, machine_id="m1")
    assert e.value.reason == "build_changed"
    with pytest.raises(PlanRefused) as e:  # a command-line tool or an API key is not installed by this
        build_plan({"id": "cli-x", "origin": "installed_cli"}, facts, CAT, RECIPES, RULES, machine_id="m1")
    assert e.value.reason == "no_recipe"


def test_a_split_model_gets_one_download_step_per_shard():
    facts = machine_facts("linux", "amd64", "cpu", ram_total=128.0, ram_free=100.0)
    row = next(r for r in CAT["model_files"] if r.get("shards") and r["params_b"] < 9 and r["kind"] == "chat" and r["offer_by_default"])
    opt = next(o for o in O.build_options(facts, choice="local", catalog={**CAT, "model_files": [row]})["options"])
    plan = build_plan(opt, facts, {**CAT, "model_files": [row]}, RECIPES, RULES, machine_id="m1")
    fetches = [s for s in plan.steps if s.op == "fetch_check"]
    assert len(fetches) == 1 + len(row["shards"]) and [s.args["sha256"] for s in fetches[1:]] == [sh["sha256"] for sh in row["shards"]]
    assert plan.steps[-3].args["model"].endswith(row["shards"][0]["file"])  # the server is started on the first shard, which names the others


def test_a_vision_models_plan_downloads_its_projector_and_starts_the_server_with_it_and_the_numbers_count_it():
    row = min((r for r in CAT["model_files"] if r["kind"] == "vision-language" and r.get("companions") and r["offer_by_default"]), key=lambda r: r["bytes"])
    cat = {**CAT, "model_files": [row]}
    facts = machine_facts("linux", "amd64", "cpu", ram_total=128.0, ram_free=100.0)
    opt = O.build_options(facts, choice="local", catalog=cat)["options"][0]
    assert any("includes vision" in x for x in opt["labels"]) and opt["companions"] == row["companions"]
    plan = build_plan(opt, facts, cat, RECIPES, RULES, machine_id="m1")
    proj = row["companions"][0]
    fetches = [s for s in plan.steps if s.op == "fetch_check"]
    assert fetches[-1].args["sha256"] == proj["sha256"] and fetches[-1].args["bytes"] == proj["bytes"]
    start = next(s for s in plan.steps if s.op == "start_server")
    assert "--mmproj" in start.exact_command and start.args["mmproj"].endswith(proj["file"]) and next(s for s in plan.steps if s.op == "write_server_config").args["mmproj"] == start.args["mmproj"]
    plain = build_plan({**opt, "companions": []}, facts, {**cat, "model_files": [{**row, "companions": []}]}, RECIPES, RULES, machine_id="m1")
    assert "--mmproj" not in next(s for s in plain.steps if s.op == "start_server").exact_command and len(plain.steps) == len(plan.steps) - 1


# ---- REAL captures: the two machines of this project ----------------------------------------------------------------------------------------------------------------------------------------------------

def test_the_real_windows_machine_gets_the_cpu_build_a_powershell_plan_and_no_layers_on_the_graphics_chip():
    """REAL capture (Windows 11, Intel integrated graphics that shares RAM, models run on the CPU, 8.2 GiB free)."""
    doc = json.loads((ROOT / "fixtures" / "probes" / "windows-real" / "typed_facts.redacted.json").read_text())
    facts = {k: row["value"] for k, row in doc["facts"].items()}
    opt, plan = first_plan(facts)
    assert opt["build"]["cell"] == "windows-cpu" and opt["build"]["backend"] == "cpu" and opt["build"]["file"].endswith("win-cpu-x64.zip")
    by = {s.op: s for s in plan.steps}
    assert "Invoke-WebRequest" in plan.steps[0].exact_command and "Expand-Archive" in by["unpack_archive"].exact_command and "llama-server.exe" in by["start_server"].exact_command
    assert "-ngl 0" in by["start_server"].exact_command and plan.fit.limited_by.startswith("available RAM (8.2 GiB free")


def test_this_macs_real_probes_pick_the_metal_build_and_the_unified_memory_budget():
    """REAL: the probes run on THIS machine, whatever it is. On Apple silicon the answer must be Metal and the unified-memory budget; elsewhere the cell is the one for this machine's own facts."""
    from envrec_os import probes
    facts: dict = {}
    for fn in ("os_details", "gpus", "memory_for_models"):
        facts.update(getattr(probes, fn)()["facts"])
    sel = O.local_build(facts, CAT, RULES)
    if facts["machine.os.name"] == "macOS" and facts["machine.os.arch"] == "arm64":
        assert facts["machine.model_path"] == "unified" and (sel["cell"], sel["backend"], sel["memory"]) == ("macos-arm64-metal", "metal", "unified")
        budget, basis = O._memory_budget(O._norm(facts))
        assert basis.startswith("unified memory") and budget <= round(facts["machine.ram_total_gib"] * 0.66, 2)
    else:
        assert sel["ok"] or sel["reason"] in ("unsupported_cell", "no_cell_for_facts", "no_build_in_catalog")  # a typed answer, never a crash or a guess

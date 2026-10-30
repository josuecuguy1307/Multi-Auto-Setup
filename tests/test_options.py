"""Setup options: hard filters and the written ordering are code, independent of anything a model says (skill setup-options)."""
from __future__ import annotations

import copy

import pytest

from envrec_coordinator import options as O

CAT = {"runtime_assets": [
    {"id": "r-cpu", "source": "llama.cpp", "tag": "b1", "os": "windows", "arch": "amd64", "backend": "cpu", "license": "MIT", "bytes": 10 * 2 ** 20, "runnable": True},
    {"id": "r-vk", "source": "llama.cpp", "tag": "b1", "os": "windows", "arch": "amd64", "backend": "vulkan", "license": "MIT", "bytes": 20 * 2 ** 20, "runnable": True},
    {"id": "r-mac", "source": "llama.cpp", "tag": "b1", "os": "macos", "arch": "arm64", "backend": "cpu", "license": "MIT", "bytes": 10 * 2 ** 20, "runnable": True}],
    "model_files": [{"repo": "x/Small-1B-GGUF", "params_b": 1.0, "quantization": "Q4_K_M", "license": "apache-2.0", "license_source": "u", "bytes": 700 * 2 ** 20, "min_ram_gib": 1.0},
                    {"repo": "x/Mid-7B-GGUF", "params_b": 7.0, "quantization": "Q4_K_M", "license": "apache-2.0", "license_source": "u", "bytes": 4 * 2 ** 30, "min_ram_gib": 6.0},
                    {"repo": "x/Big-32B-GGUF", "params_b": 32.0, "quantization": "Q4_K_M", "license": "apache-2.0", "license_source": "u", "bytes": 19 * 2 ** 30, "min_ram_gib": 24.0},
                    {"repo": "x/Odd-3B-GGUF", "params_b": 3.0, "quantization": "Q4_K_M", "license": "research-only", "license_source": "u", "bytes": 2 * 2 ** 30, "min_ram_gib": 3.0}]}
WIN = {"machine.os.name": "Windows 11", "machine.os.arch": "amd64", "machine.model_path": "cpu", "machine.vram_gib": None, "machine.ram_total_gib": 16.0, "machine.disk_free_gib": 100.0,
       "machine.gpus": [{"vendor": "Intel", "memory": "shared"}], "machine.runtimes": [], "machine.ai_clis": [{"id": "claude", "name": "Claude Code", "set_up": True}], "machine.api_key_variable_names": []}


def build(facts=WIN, **kw):
    return O.build_options(facts, catalog=copy.deepcopy(CAT), evidence={"entries": []}, **kw)


def titles(r):
    return [o["id"] for o in r["options"]]


def test_local_choice_keeps_only_models_that_fit_memory_disk_and_license_and_names_why_the_rest_went():
    r = build(choice="local")
    assert titles(r) == ["catalog-7.0b-Mid-7B-GGUF", "catalog-1.0b-Small-1B-GGUF"]  # 16 GiB x 0.7 = 11.2: 7B (6 GiB) fits, 32B (24) does not; the research-only license is out; larger first
    why = {x["id"]: x["reason"] for x in r["removed"]}
    assert "needs 24.0 GiB" in why["catalog-32.0b-Big-32B-GGUF"] and "license" in why["catalog-3.0b-Odd-3B-GGUF"] and "not the kind you chose" in why["cli-claude"]
    assert all(o["status"] == "provisional, not measured here" and o["cloud"] is False and o["label"].startswith("verified-by-code") for o in r["options"])


def test_a_gpu_path_budgets_by_vram_not_by_ram():
    gpu = {**WIN, "machine.model_path": "gpu", "machine.vram_gib": 6.0, "machine.gpus": [{"vendor": "NVIDIA", "memory": "dedicated"}]}
    assert titles(build(gpu, choice="local")) == ["catalog-1.0b-Small-1B-GGUF"]  # 6 GiB x 0.7 = 4.2: the 7B (6 GiB) no longer fits
    assert "vulkan" in build(gpu, choice="local")["options"][0]["runtime"]  # a GPU on Windows gets the Vulkan build


def test_a_runtime_already_installed_comes_before_a_download_and_costs_nothing():
    facts = {**WIN, "machine.runtimes": [{"id": "ollama", "name": "Ollama", "models": [{"name": "qwen2.5:7b"}], "exposed_beyond_loopback": True}]}
    r = build(facts, choice="local")
    assert r["options"][0]["id"] == "use-ollama" and r["options"][0]["needs_download"] is False and any("reachable from the network" in x for x in r["options"][0]["limits"])


def test_a_command_line_tool_option_is_cloud_and_never_mentions_a_token():
    r = build(choice="cli")
    assert titles(r) == ["cli-claude"] and r["options"][0]["cloud"] is True and r["options"][0]["cost_class"] == "subscription" and any("CLOUD" in x for x in r["options"][0]["limits"])
    assert "token" not in repr(r["options"][0]["limits"]).replace("nothing is read", "")


def test_confidential_work_removes_every_cloud_option():
    r = build(choice="mix", sensitivity="confidential")
    assert all(not o["cloud"] for o in r["options"])
    assert any(x["reason"].startswith("sends data to a provider") for x in r["removed"])
    assert build(choice="cli", sensitivity="confidential")["options"] == []  # nothing left is said by an empty list, not invented


def test_the_order_is_cost_class_then_written_order_and_does_not_depend_on_the_input_order():
    facts = {**WIN, "machine.api_key_variable_names": [{"provider": "openai", "name": "OPENAI_API_KEY"}]}
    r = build(facts, choice="mix")
    assert [o["kind"] for o in r["options"]] == ["local", "cli", "api"] and r["options"][2]["key_variable_found"] is True
    reversed_ = list(reversed(O.candidates(facts, copy.deepcopy(CAT), O.load_harnesses())))
    assert [o["id"] for o in O.order_options([o for o in reversed_ if o["id"] in ("api-openai", "cli-claude", "catalog-7.0b-Mid-7B-GGUF")])] == ["catalog-7.0b-Mid-7B-GGUF", "cli-claude", "api-openai"]


def test_a_measured_pass_rate_would_come_before_the_written_order_within_a_cost_class():
    a = {"id": "a", "origin": "catalog_runtime", "cost_class": "free_local", "title": "a", "params_b": 1.0}
    b = {"id": "b", "origin": "catalog_runtime", "cost_class": "free_local", "title": "b", "params_b": 1.0, "measured": {"pass_rate": 0.9, "seconds": 5}}
    c = {"id": "c", "origin": "installed_runtime", "cost_class": "free_local", "title": "c", "params_b": 1.0}
    assert [o["id"] for o in O.order_options([a, c, b])] == ["b", "c", "a"]


def test_at_most_three_options_are_shown_and_the_rule_is_stated():
    r = build(choice="mix")
    assert len(r["options"]) <= 3 and "cost class" in r["rule"]
    many = build({**WIN, "machine.api_key_variable_names": []}, choice="api")
    assert len(many["options"]) == 3 and all(o["kind"] == "api" for o in many["options"])


def test_an_operating_system_with_no_runtime_build_gets_no_local_option_and_an_unknown_arch_too():
    assert [o for o in build({**WIN, "machine.os.arch": "riscv64"}, choice="local")["options"]] == []
    assert [o for o in build({**WIN, "machine.os.name": "Plan9"}, choice="local")["options"]] == []


def test_evidence_lookup_says_no_public_data_when_there_is_none():
    assert O.evidence_for({"entries": []}, "llama.cpp") == {"entries": [], "note": "no public data"}
    e = O.evidence_for({"entries": [{"topic": "llama.cpp", "source": "s", "tier": 2, "date": "2026-01-01"}]}, "llama.cpp")
    assert e["entries"] and e["note"] == ""


def test_the_real_catalog_has_the_fields_the_builder_needs_and_licenses_are_on_the_allowed_list():
    cat = O.load_catalog()
    assert cat["runtime_assets"] and cat["model_files"]
    assert all({"os", "arch", "backend", "license", "bytes"} <= set(a) for a in cat["runtime_assets"])
    assert all({"repo", "params_b", "quantization", "license", "bytes", "min_ram_gib"} <= set(m) for m in cat["model_files"])
    assert {m["license"] for m in cat["model_files"]} <= set(O.load_harnesses()["licenses_allowed"])
    real = O.build_options(WIN, choice="local")
    assert real["options"] and real["options"][0]["origin"] == "catalog_runtime"


@pytest.mark.parametrize("bad", ["", "everything"])
def test_an_unknown_choice_keeps_nothing(bad):
    assert build(choice=bad)["options"] == []

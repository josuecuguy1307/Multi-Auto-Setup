"""Setup options: hard filters and the written ordering are code, independent of anything a model says (skill setup-options)."""
from __future__ import annotations

import copy

import pytest

from envrec_coordinator import options as O

VERIFIED = {"label": "verified-by-code"}  # what the lock writes once a row's header and listing were read (rule R7.2)
CAT = {"runtime_assets": [
    {"id": "r-cpu", "source": "llama.cpp", "tag": "b1", "os": "windows", "arch": "amd64", "backend": "cpu", "license": "MIT", "bytes": 10 * 2 ** 20, "runnable": True},
    {"id": "r-vk", "source": "llama.cpp", "tag": "b1", "os": "windows", "arch": "amd64", "backend": "vulkan", "license": "MIT", "bytes": 20 * 2 ** 20, "runnable": True},
    {"id": "r-mac", "source": "llama.cpp", "tag": "b1", "os": "macos", "arch": "arm64", "backend": "cpu", "license": "MIT", "bytes": 10 * 2 ** 20, "runnable": True}],
    "model_files": [{"id": "x/Small-1B-GGUF/s.gguf", "repo": "x/Small-1B-GGUF", "params_b": 1.0, "quantization": "Q4_K_M", "license": "apache-2.0", "license_source": "u", "bytes": 700 * 2 ** 20, "min_ram_gib": 1.0, "verified": VERIFIED},
                    {"id": "x/Mid-7B-GGUF/m.gguf", "repo": "x/Mid-7B-GGUF", "params_b": 7.0, "quantization": "Q4_K_M", "license": "apache-2.0", "license_source": "u", "bytes": 4 * 2 ** 30, "min_ram_gib": 6.0, "verified": VERIFIED},
                    {"id": "x/Big-32B-GGUF/b.gguf", "repo": "x/Big-32B-GGUF", "params_b": 32.0, "quantization": "Q4_K_M", "license": "apache-2.0", "license_source": "u", "bytes": 19 * 2 ** 30, "min_ram_gib": 24.0, "verified": VERIFIED},
                    {"id": "x/Odd-3B-GGUF/o.gguf", "repo": "x/Odd-3B-GGUF", "params_b": 3.0, "quantization": "Q4_K_M", "license": "research-only", "license_source": "u", "bytes": 2 * 2 ** 30, "min_ram_gib": 3.0, "verified": VERIFIED}]}
WIN = {"machine.os.name": "Windows 11", "machine.os.arch": "amd64", "machine.model_path": "cpu", "machine.vram_gib": None, "machine.ram_total_gib": 16.0, "machine.disk_free_gib": 100.0,
       "machine.gpus": [{"vendor": "Intel", "memory": "shared"}], "machine.runtimes": [], "machine.ai_clis": [{"id": "claude", "name": "Claude Code", "set_up": True}], "machine.api_key_variable_names": []}


def build(facts=WIN, **kw):
    return O.build_options(facts, catalog=copy.deepcopy(CAT), evidence={"entries": []}, **kw)


def titles(r):
    return [o["id"] for o in r["options"]]


def test_local_choice_keeps_only_models_that_fit_memory_disk_and_license_and_names_why_the_rest_went():
    r = build(choice="local")
    assert titles(r) == ["catalog-7.0b-Mid-7B-GGUF-q4_k_m", "catalog-1.0b-Small-1B-GGUF-q4_k_m"]  # 16 GiB x 0.7 = 11.2: 7B (5.4 GiB: 4 of weights + 1 fixed + 10%) fits, 32B (21.9) does not; the research-only license is out; larger first
    why = {x["id"]: x["reason"] for x in r["removed"]}
    assert "needs 21.9 GiB" in why["catalog-32.0b-Big-32B-GGUF-q4_k_m"] and "license" in why["catalog-3.0b-Odd-3B-GGUF-q4_k_m"] and "not the kind you chose" in why["cli-claude"]
    assert all(o["status"] == "provisional, not measured here" and o["cloud"] is False and o["label"].startswith("verified-by-code") for o in r["options"])


def test_a_gpu_path_budgets_by_vram_not_by_ram():
    gpu = {**WIN, "machine.model_path": "gpu", "machine.vram_gib": 6.0, "machine.gpus": [{"vendor": "NVIDIA", "memory": "dedicated"}]}
    assert titles(build(gpu, choice="local")) == ["catalog-1.0b-Small-1B-GGUF-q4_k_m"]  # 6 GiB x 0.7 = 4.2: the 7B (5.4 GiB) no longer fits
    assert "vulkan" in build(gpu, choice="local")["options"][0]["runtime"]  # a GPU on Windows gets the Vulkan build


def test_a_runtime_already_installed_that_has_no_setup_recipe_is_not_offered_it_gets_a_typed_no_recipe_yet():
    facts = {**WIN, "machine.runtimes": [{"id": "ollama", "name": "Ollama", "models": [{"name": "qwen2.5:7b"}], "exposed_beyond_loopback": True}]}
    r = build(facts, choice="local")
    assert "use-ollama" not in titles(r) and titles(r)[0] == "catalog-7.0b-Mid-7B-GGUF-q4_k_m"  # only what a recipe can set up is offered
    why = {x["id"]: x["reason"] for x in r["removed"]}
    assert why["use-ollama"].startswith("no_recipe_yet:") and "Ollama" in why["use-ollama"]


def test_a_catalog_runtime_with_no_setup_recipe_is_not_offered_either():
    opt = {"id": "catalog-x", "kind": "local", "origin": "catalog_runtime", "cloud": False, "runtime_source": "some-other-runtime", "runtime": "some-other-runtime b1 (cpu)", "license": "mit", "memory_needed_gib": 1.0, "disk_needed_gib": 1.0}
    kept, removed = O.apply_filters([opt], WIN, "local", "internal", rules=O.load_rules(), catalog=CAT)
    assert kept == [] and removed[0]["removed_by"].startswith("no_recipe_yet:") and "some-other-runtime" in removed[0]["removed_by"]
    kept, _ = O.apply_filters([{**opt, "runtime_source": "llama.cpp"}], WIN, "local", "internal", rules=O.load_rules(), catalog=CAT)
    assert len(kept) == 1  # the same option from the runtime the recipe is for is kept


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
    assert [o["id"] for o in O.order_options([o for o in reversed_ if o["id"] in ("api-openai", "cli-claude", "catalog-7.0b-Mid-7B-GGUF-q4_k_m")])] == ["catalog-7.0b-Mid-7B-GGUF-q4_k_m", "cli-claude", "api-openai"]


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
    assert all(O.load_rules().license_offered(m["license"]) for m in cat["model_files"])  # every license in the lock is on the table of known licenses, in an offered class
    real = O.build_options(WIN, choice="local")
    assert real["options"] and real["options"][0]["origin"] == "catalog_runtime"


@pytest.mark.parametrize("bad", ["", "everything"])
def test_an_unknown_choice_keeps_nothing(bad):
    assert build(choice=bad)["options"] == []


def _windows_real():
    import json
    from pathlib import Path
    doc = json.loads((Path(__file__).resolve().parent.parent / "fixtures" / "probes" / "windows-real" / "typed_facts.redacted.json").read_text())
    return {k: row["value"] for k, row in doc["facts"].items()}


def test_the_memory_budget_is_the_available_ram_less_a_margin_labeled_with_the_measured_value():
    f = _windows_real()  # REAL capture: 19.7 GiB total, 8.2 GiB available, models run on cpu
    budget, name = O._memory_budget(O._norm(f))
    assert budget == round(8.2 * (1 - O.load_rules().param("available_margin")), 2) == 6.97 and name == "available RAM (8.2 GiB free when measured, 15% margin)"
    built = O.build_options(f, choice="local")
    assert all(o["memory_needed_gib"] <= 6.97 for o in built["options"] if o.get("memory_needed_gib") is not None)
    assert not any("mmproj" in o.get("model", "") + o["title"] and "VL" in o["title"] for o in built["options"])  # the vision projector files were never models: not offered
    tight = {**f, "machine.ram_available_gib": 1.0}  # a measured 1.0 GiB free: the budget is 0.85 GiB and the removal says what was measured
    removed = [r["reason"] for r in O.build_options(tight, choice="local")["removed"]]
    assert any("only 0.85 GiB of available RAM (1.0 GiB free when measured, 15% margin) may be used for a model" in r for r in removed), removed[:3]
    o = built["options"][0]
    assert "budget 6.97 GiB from available RAM (8.2 GiB free when measured, 15% margin); the analysis says models run on: cpu" in o["memory_basis"]


def test_when_the_available_figure_is_missing_the_total_share_is_used_and_said_so():
    f = {k: v for k, v in _windows_real().items() if k != "machine.ram_available_gib"}
    budget, name = O._memory_budget(O._norm(f))
    assert budget == round(19.7 * 0.7, 2) and name == "RAM (19.7 GiB total measured; the available figure was missing)"


def test_a_row_whose_header_was_never_read_is_not_offered_and_the_projector_rule_lives_in_the_rules_data():
    """The first real pick (an '8B Q8_0') was a 0.7 GiB vision projector: the lock had never been checked against the files. Rows without a `verified` block are held back (R7.2)."""
    from envrec_coordinator import options as O
    h = O.load_harnesses()
    assert "R1.1-projector" in O.load_rules().ids() and "not_standalone_model_files" not in h  # the rule is data in the skill, not a list in a JSON beside the code
    cat = {"runtime_assets": [{"os": "windows", "arch": "amd64", "backend": "cpu", "runnable": True, "source": "llama.cpp", "tag": "t", "bytes": 1, "license": "mit"}],
           "model_files": [{"repo": "x/Real-8B-GGUF", "file": "real-8b-q4.gguf", "params_b": 8.0, "min_ram_gib": 6.0, "bytes": 1, "license": "apache-2.0", "license_source": "s", "quantization": "Q4"},
                           {"repo": "x/Real-8B-GGUF", "file": "mmproj-real-8b-q8.gguf", "params_b": 8.0, "min_ram_gib": 1.2, "bytes": 1, "license": "apache-2.0", "license_source": "s", "quantization": "Q8_0"}]}
    facts = {"machine.os.name": "Windows 11", "machine.os.arch": "amd64", "machine.model_path": "cpu", "machine.ram_total_gib": 16.0, "machine.disk_free_gib": 100.0}
    built = O.build_options(facts, choice="local", catalog=cat, evidence={"entries": []})
    assert built["options"] == [] and sum("header was not read" in r["reason"] for r in built["removed"]) == 2
    silent = O.build_options({k: v for k, v in facts.items() if k != "machine.model_path"}, choice="local", catalog=cat, evidence={"entries": []})  # where models run was not measured: no guess
    assert silent["options"] == [] and silent["removed"][0]["id"] == "local-runtime" and silent["removed"][0]["reason"].startswith("no_cell_for_facts")


def test_the_shipped_lock_has_no_file_that_is_not_a_model_and_every_row_was_checked_against_its_header():
    cat = O.load_catalog()
    rows = cat["model_files"]
    assert rows and all(r.get("verified", {}).get("label") == "verified-by-code" for r in rows)
    assert {r["kind"] for r in rows} <= {"chat", "base", "no_template", "embedding", "reranker", "vision-language", "speech", "draft", "guard", "other-modality"}  # a projector, adapter, vocab or auxiliary file is never a row
    assert not [r["file"] for r in rows if "mmproj" in r["file"].lower() or "imatrix" in r["file"].lower()]
    assert all(r["revision"] and len(r["revision"]) == 40 and len(r["sha256"]) == 64 for r in rows)


def test_the_stored_parameter_count_agrees_with_the_real_file_size():
    """A row that says 5.0B for a 0.37 GiB file (the 0.5B model whose name was read as '5b') cannot pass: bits per weight is bytes x 8 / parameters, and every quantization here is 4 to 9."""
    bad = [(r["id"], r["params_b"], round(r["bytes"] * 8 / (r["params_b"] * 1e9), 2)) for r in O.load_catalog()["model_files"] if not 3.5 <= r["bytes"] * 8 / (r["params_b"] * 1e9) <= 9.5]
    assert bad == []


def test_the_real_windows_machine_is_offered_a_cpu_build_and_chat_models_that_fit_with_verified_numbers():
    """SIMULATED-LOCAL on a REAL capture (Windows 11, 19.7 GiB RAM, 8.2 free, an integrated Intel GPU that shares RAM): the options after the catalog was re-checked."""
    f = _windows_real()
    built = O.build_options(f, choice="local")
    assert built["options"]
    for o in built["options"]:
        assert "(cpu)" in o["runtime"] and "vulkan" not in o["runtime"]  # integrated GPU: the CPU build (R3.2)
        assert o["model_kind"] in ("chat", "vision-language") and o["requires_task"] == {} and o["memory_needed_gib"] <= 6.97
        row = next(r for r in O.load_catalog()["model_files"] if r["id"] == o["model_id"])
        assert o["params_b"] == row["params_b"] and o["model_sha256"] == row["sha256"] and o["model_bytes"] == row["bytes"]
        assert "estimate" in o["memory_basis"] and "KV cache" in o["memory_basis"]
    cat = O.load_catalog()
    removed = O.apply_filters(O.candidates(f, cat, O.load_harnesses()), f, "local", "internal", catalog=cat)[1]  # the whole list: the one a result shows is cut at 60
    by = " | ".join(r["removed_by"] for r in removed)
    for needle in ("a speech or audio model", "an embedding model produces vectors", "a base model cannot take chat turns", "the file carries no chat template", "a guard model classifies text", "only 6.97 GiB of available RAM"):
        assert needle in by, needle  # each thing that is not a general chat model on this machine says why it is not offered
    assert not any("mmproj" in r["id"].lower() for r in built["removed"])  # a projector is not even a candidate any more


def test_every_option_id_is_unique_even_when_one_repository_has_several_quantizations():
    from envrec_coordinator import options as O
    facts = {"machine.os.name": "Windows 11", "machine.os.arch": "amd64", "machine.ram_total_gib": 64.0, "machine.ram_available_gib": 60.0, "machine.disk_free_gib": 500.0, "machine.model_path": "cpu"}
    ids = [o["id"] for o in O.candidates(facts, O.load_catalog(), O.load_harnesses())]
    assert len(ids) == len(set(ids)), [i for i in ids if ids.count(i) > 1][:3]


def _llama_cat(lic, **over):
    row = {"id": "x/L-8B-GGUF/l.gguf", "repo": "x/L-8B-GGUF", "params_b": 8.0, "quantization": "Q4_K_M", "license": lic, "license_source": "u", "bytes": 5 * 2 ** 30, "verified": VERIFIED, "kind": "chat", "labels": []}
    return {"runtime_assets": CAT["runtime_assets"], "model_files": [{**row, **over}]}


def test_a_meta_llama_community_license_is_a_known_license_and_the_option_records_its_name_and_class():
    cat = _llama_cat("llama3.1", license_name="Llama 3.1 Community License", license_class="community-host-run", license_label="community license", license_basis="declared-by-project: the card only")
    facts = {**WIN, "machine.ram_total_gib": 32.0}
    r = O.build_options(facts, choice="local", catalog=cat, evidence={"entries": []})
    [o] = r["options"]
    assert o["license"] == "llama3.1" and o["license_name"] == "Llama 3.1 Community License" and o["license_class"] == "community-host-run" and "declared-by-project" in o["license_basis"]


def test_a_license_the_table_does_not_know_is_still_removed_with_its_reason():
    r = O.build_options({**WIN, "machine.ram_total_gib": 32.0}, choice="local", catalog=_llama_cat("gemma"), evidence={"entries": []})
    assert r["options"] == [] and "not a known license whose class is offered" in r["removed"][0]["reason"]


def test_a_five_gib_model_has_no_size_cap_it_is_offered_when_it_fits_and_removed_with_the_numbers_when_it_does_not():
    cat = _llama_cat("apache-2.0")
    roomy = O.build_options({**WIN, "machine.ram_total_gib": 32.0, "machine.ram_available_gib": 28.0}, choice="local", catalog=cat, evidence={"entries": []})
    assert [o["model"] for o in roomy["options"]] == ["x/L-8B-GGUF"] and roomy["options"][0]["memory_needed_gib"] == 6.5  # 5 GiB of weights + 1 fixed + 10%
    tight = O.build_options({**WIN, "machine.ram_total_gib": 8.0, "machine.ram_available_gib": 6.0}, choice="local", catalog=cat, evidence={"entries": []})
    assert tight["options"] == [] and "needs 6.5 GiB and only 5.1 GiB of available RAM" in tight["removed"][0]["reason"]


@pytest.mark.parametrize("name,family", [("macOS", "macos"), ("Windows 11", "windows"), ("Linux", "linux"), ("Ubuntu", "linux"), ("Debian GNU/Linux", "linux"), ("Fedora Linux", "linux"), ("Arch Linux", "linux"),
                                         ("Red Hat Enterprise Linux", "linux"), ("Raspberry Pi OS", "linux"), ("FreeBSD", ""), ("Plan9", ""), ("", ""), (None, "")])
def test_an_operating_system_is_a_family_from_the_name_when_the_machine_did_not_say_and_never_a_guess_when_the_name_is_unknown(name, family):
    assert O._norm({"machine.os.name": name})["os"] == family  # on real Linux the name is the distribution's: it used to be read as no family at all


def test_a_machine_that_reports_its_own_family_is_believed_over_its_name():
    assert O._norm({"machine.os.name": "SomeNewDistro", "machine.os.family": "linux"})["os"] == "linux"


def test_the_os_probe_reports_the_family_on_this_machine_and_it_is_the_family_of_the_per_os_layer():
    import envrec_os
    from envrec_os import probes
    f = probes.os_details()["facts"]
    assert f["machine.os.family"] == {"darwin": "macos", "linux": "linux", "windows": "windows"}[envrec_os.family()]
    assert O._norm(f)["os"] == f["machine.os.family"]


@pytest.mark.parametrize("fam,expected", [("linux", "linux"), ("windows", "windows"), ("darwin", "macos")])
def test_the_os_probe_reports_the_family_of_every_branch_not_only_this_machines(fam, expected, monkeypatch, tmp_path):
    """SIMULATED-LOCAL: the Linux and Windows branches of the probe run here with the platform calls replaced by what those systems answer."""
    import platform
    from envrec_os import probes
    monkeypatch.setattr(probes, "family", lambda: fam)
    monkeypatch.setattr(platform, "release", lambda: {"linux": "6.8.0-45-generic", "windows": "11", "darwin": "24.6.0"}[fam])
    monkeypatch.setattr(platform, "machine", lambda: {"linux": "aarch64", "windows": "AMD64", "darwin": "arm64"}[fam])
    monkeypatch.setattr(platform, "version", lambda: "10.0.26100")
    monkeypatch.setattr(platform, "mac_ver", lambda: ("15.0", ("", "", ""), ""))
    if fam == "windows":
        monkeypatch.setattr(probes, "_windows_version", lambda release: "Windows 11")
    f = probes.os_details()["facts"]
    assert f["machine.os.family"] == expected and O._norm(f)["os"] == expected
    assert O._norm(f)["arch"] == {"linux": "arm64", "windows": "amd64", "darwin": "arm64"}[fam]  # aarch64 / AMD64 / arm64 all become the catalog's two names


def test_a_vision_model_is_offered_when_it_fits_with_its_projector_counted_and_says_it_includes_vision():
    row = {"id": "x/V-4B-GGUF/v.gguf", "repo": "x/V-4B-GGUF", "params_b": 4.0, "quantization": "Q4_K_M", "license": "apache-2.0", "license_source": "u", "bytes": 2 * 2 ** 30, "verified": VERIFIED, "kind": "vision-language",
           "labels": ["includes vision (the projector is counted in the memory and disk numbers)"], "companions": [{"file": "mmproj-v.gguf", "bytes": 2 ** 29, "kind": "vision-projector"}]}
    cat = {"runtime_assets": CAT["runtime_assets"], "model_files": [row]}
    plain = O.build_options({**WIN, "machine.ram_total_gib": 32.0}, choice="local", catalog={"runtime_assets": CAT["runtime_assets"], "model_files": [{**row, "companions": [], "labels": []}]}, evidence={"entries": []})
    seen = O.build_options({**WIN, "machine.ram_total_gib": 32.0}, choice="local", catalog=cat, evidence={"entries": []})
    [o], [p] = seen["options"], plain["options"]
    assert any("includes vision" in x for x in o["labels"]) and abs((o["memory_needed_gib"] - p["memory_needed_gib"]) - 0.55) < 0.02 and o["disk_needed_gib"] > p["disk_needed_gib"]  # 0.5 GiB of projector, and the 10% runtime share of it
    assert o["requires_task"] == {} and o["companions"] == row["companions"]

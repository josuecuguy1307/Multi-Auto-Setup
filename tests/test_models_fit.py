"""P5b: the new local models through the EXISTING generator and verifier (per-repo approvals in the manifest, GGUF header, the pinned llama.cpp build's architectures, license table), and what fits a Lenovo-like
computer (about 7.8 GiB usable, 15% margin). REAL: the hub listings and headers were read on 2026-11-02 (scripts/catalog_add.py, scripts/catalog_verify.py); no weights were downloaded. What failed stays out with its reason;
the model used only to build or test the project is verified apart and waits for the owner's decision (it is not in the shipped lock)."""
from __future__ import annotations

from pathlib import Path

import yaml

from envrec.catalog import rules as R
from envrec_coordinator import options as O

ROOT = Path(__file__).resolve().parents[1]
LOCK = yaml.safe_load((ROOT / "catalog.lock.yaml").read_text())
ROWS = {m["id"].split("/")[-1]: m for m in LOCK["model_files"]}
RULES = R.Rules.load()
FACTS = {"machine.os.name": "Windows 11", "machine.os.family": "windows", "machine.os.arch": "amd64", "machine.model_path": "cpu", "machine.vram_gib": None, "machine.ram_total_gib": 15.7, "machine.ram_available_gib": 9.2,
         "machine.disk_free_gib": 200.0, "machine.gpus": [{"vendor": "Intel", "memory": "shared"}], "machine.runtimes": [], "machine.ai_clis": [], "machine.api_key_variable_names": []}


def test_the_budget_of_a_lenovo_like_computer_is_about_7_8_gib_with_the_margin():
    budget, basis = O._memory_budget(O._norm(FACTS))
    assert budget == 7.82 and "15% margin" in basis


def test_llama_3_2_3b_and_gemma_4_e4b_entered_through_the_generator_verified_by_code():
    for name, arch, lic, cls in (("Llama-3.2-3B-Instruct-Q4_K_M.gguf", "llama", "llama3.2", "community-host-run"), ("Llama-3.2-3B-Instruct-Q8_0.gguf", "llama", "llama3.2", "community-host-run"),
                                 ("Llama-3.2-3B-Instruct-Q4_0.gguf", "llama", "llama3.2", "community-host-run"), ("gemma-4-E4B_q4_0-it.gguf", "gemma4", "apache-2.0", "permissive")):
        m = ROWS[name]
        assert m["verified"]["label"] == "verified-by-code" and m["architecture"] == arch and m["license"] == lic and m["license_class"] == cls and len(m["sha256"]) == 64 and m["revision"] in m["url"]
    assert ROWS["Llama-3.2-3B-Instruct-Q4_K_M.gguf"]["params_b"] == 3.21 and ROWS["gemma-4-E4B_q4_0-it.gguf"]["params_b"] == 7.46  # the header's count, not the name's ("E4B" is the effective size)
    assert ROWS["Llama-3.2-3B-Instruct-Q4_K_M.gguf"]["base_model"] == "meta-llama/Llama-3.2-3B-Instruct"


def test_fit_numbers_for_the_lenovo_like_budget_say_what_fits_and_what_does_not():
    budget, _ = O._memory_budget(O._norm(FACTS))
    need = {n: RULES_NEED for n, RULES_NEED in ((n, R.estimate_memory(ROWS[n], RULES, context=4096)["total_gib"]) for n in
            ("Llama-3.2-3B-Instruct-Q4_0.gguf", "Llama-3.2-3B-Instruct-Q4_K_M.gguf", "Llama-3.2-3B-Instruct-Q8_0.gguf", "gemma-4-E4B_q4_0-it.gguf", "Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf",
             "Meta-Llama-3.1-8B-Instruct-Q8_0.gguf", "granite-4.1-8b-Q4_K_M.gguf", "granite-4.1-8b-Q8_0.gguf"))}
    assert need == {"Llama-3.2-3B-Instruct-Q4_0.gguf": 3.41, "Llama-3.2-3B-Instruct-Q4_K_M.gguf": 3.51, "Llama-3.2-3B-Instruct-Q8_0.gguf": 4.94, "gemma-4-E4B_q4_0-it.gguf": 6.94,
                    "Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf": 6.54, "Meta-Llama-3.1-8B-Instruct-Q8_0.gguf": 10.25, "granite-4.1-8b-Q4_K_M.gguf": 7.1, "granite-4.1-8b-Q8_0.gguf": 11.2}
    assert {n for n, v in need.items() if v > budget} == {"Meta-Llama-3.1-8B-Instruct-Q8_0.gguf", "granite-4.1-8b-Q8_0.gguf"}  # these two do not fit and are said not to, with the numbers
    res = O.build_options(FACTS, choice="local", sensitivity="internal", prefer_publisher=None)
    removed = {r["id"]: r["reason"] for r in res["removed"]}
    gone = next(v for k, v in removed.items() if "Llama-3.1-8B" in k and k.endswith("q8_0"))
    assert "needs 10.25 GiB and only 7.82 GiB" in gone


def test_what_failed_stays_out_with_its_reason_and_the_build_only_model_is_not_shipped():
    ex = {e["what"]: e["reason"] for e in LOCK["excluded"]}
    for repo in ("nvidia/NVIDIA-Nemotron-3.5-Bolt-E4B-4GB", "LiquidAI/LFM3-1.7B-GGUF"):
        assert "not a known license" in ex[repo] and not any(repo in m["id"] for m in LOCK["model_files"])
    assert not any("build-only-name-absent" in m["id"] for m in LOCK["model_files"])  # verified apart (kept locally), waiting for the owner's decision
    manifest = yaml.safe_load((ROOT / "config" / "runtime_manifest.yaml").read_text())
    by = {s["id"]: s for s in manifest["model_sources"]}
    assert by["bartowski"]["repos"] == ["bartowski/Meta-Llama-3.1-8B-Instruct-GGUF", "bartowski/Llama-3.2-3B-Instruct-GGUF"] and by["google"]["repos"] == ["google/gemma-4-E4B-it-qat-q4_0-gguf"]
    assert all(by[k]["status"] == "approve" and by[k]["repos"] for k in ("bartowski", "google", "liquidai", "nvidia"))  # approved for named repos only

"""Re-checking a lockfile against the hub and the headers: a fake hub serves listings and tiny GGUF headers; nothing touches the network."""
from __future__ import annotations

import copy

import pytest

from envrec.catalog import gguf as G
from envrec.catalog.hub import HubError
from envrec.catalog.rules import Rules
from envrec.catalog.verify import fetch_runtime_arches, rules_digest, verify_lock
from gguf_fixtures import STR, gguf, kv, model

REV = "a" * 40
SHA = {"qwen": "1" * 64, "proj": "2" * 64, "vl": "3" * 64, "gated": "4" * 64, "lic": "5" * 64, "bad": "6" * 64}
RULES = Rules.load()


class FakeHub:
    def __init__(self, repos: dict, headers: dict, broken: set | None = None):
        self.repos, self.headers, self.broken = repos, headers, broken or set()

    def repo_info(self, repo, revision):
        if repo in self.broken:
            raise HubError(f"HTTP 500 for {repo}")
        return self.repos[repo]["info"]

    def tree(self, repo, revision):
        return self.repos[repo]["tree"]

    def range_source(self, repo, revision, path):
        return G.BytesSource(self.headers[(repo, path)])


def _info(author="Org", gated=False, lic="apache-2.0", tags=None, pipeline="text-generation"):
    return {"author": author, "gated": gated, "card_license": lic, "tags": tags or [], "pipeline_tag": pipeline, "base_models": []}


def _row(repo, file, bytes_, sha, params, q="Q4_K_M"):
    return {"id": f"{repo}/{file}", "source": "org", "repo": repo, "file": file, "url": f"https://huggingface.co/{repo}/resolve/{REV}/{file}", "revision": REV, "sha256": sha, "bytes": bytes_,
            "license": "apache-2.0", "license_source": "u", "format": "gguf", "quantization": q, "params_b": params, "min_ram_gib": 1.0}


def _world():
    proj = gguf([kv("general.architecture", STR, "clip"), kv("general.type", STR, "mmproj")], [("v.w", (8, 8))])
    lic = gguf([kv("general.architecture", STR, "llama"), kv("general.license", STR, "mit"), kv("tokenizer.chat_template", STR, "t")], [("w", (4, 4))])
    withlic = gguf([kv("general.architecture", STR, "llama"), kv("general.license", STR, "apache-2.0"), kv("tokenizer.chat_template", STR, "t")], [("w", (4, 4))])
    repos = {
        "Org/Small-0.5B-GGUF": {"info": _info(), "tree": {"small-q4_k_m.gguf": {"size": 1000, "sha256": SHA["qwen"]}}},
        "Org/Vis-GGUF": {"info": _info(pipeline="image-text-to-text"), "tree": {"vl-q4_k_m.gguf": {"size": 2000, "sha256": SHA["vl"]}, "mmproj-vl-q8_0.gguf": {"size": 500, "sha256": SHA["proj"]}}},
        "Org/Gated-GGUF": {"info": _info(gated="auto"), "tree": {"g-q4_k_m.gguf": {"size": 1000, "sha256": SHA["gated"]}}},
        "Org/Lic-GGUF": {"info": _info(lic="apache-2.0"), "tree": {"l-q4_k_m.gguf": {"size": 1000, "sha256": SHA["lic"]}}},
        "Org/Moved-GGUF": {"info": _info(), "tree": {"m-q4_k_m.gguf": {"size": 1000, "sha256": "f" * 64}}},
        "Org/Orphan-GGUF": {"info": _info(), "tree": {"mmproj-o-q8_0.gguf": {"size": 400, "sha256": SHA["bad"]}}},
        "Intruder/Copy-GGUF": {"info": _info(author="Intruder"), "tree": {"c-q4_k_m.gguf": {"size": 1000, "sha256": SHA["qwen"]}}},
    }
    headers = {("Org/Small-0.5B-GGUF", "small-q4_k_m.gguf"): gguf(
        [kv("general.architecture", STR, "qwen2"), kv("general.license", STR, "apache-2.0"), kv("tokenizer.chat_template", STR, "t")], [("token_embd.weight", (100_000, 1000))]),
        ("Org/Vis-GGUF", "vl-q4_k_m.gguf"): gguf([kv("general.architecture", STR, "qwen3vl"), kv("general.license", STR, "apache-2.0"), kv("tokenizer.chat_template", STR, "t")], [("w", (100, 100))]),
        ("Org/Vis-GGUF", "mmproj-vl-q8_0.gguf"): proj, ("Org/Gated-GGUF", "g-q4_k_m.gguf"): withlic, ("Org/Lic-GGUF", "l-q4_k_m.gguf"): lic,
        ("Org/Moved-GGUF", "m-q4_k_m.gguf"): withlic, ("Org/Orphan-GGUF", "mmproj-o-q8_0.gguf"): proj, ("Intruder/Copy-GGUF", "c-q4_k_m.gguf"): withlic}
    lock = {"version": 1, "manifest_sha256": "x", "counts": {}, "runtime_assets": [], "excluded": [{"what": "old", "reason": "old"}],
            "model_files": [_row("Org/Small-0.5B-GGUF", "small-q4_k_m.gguf", 1000, SHA["qwen"], 5.0), _row("Org/Vis-GGUF", "vl-q4_k_m.gguf", 2000, SHA["vl"], 2.0),
                            _row("Org/Vis-GGUF", "mmproj-vl-q8_0.gguf", 500, SHA["proj"], 2.0, "Q8_0"), _row("Org/Gated-GGUF", "g-q4_k_m.gguf", 1000, SHA["gated"], 1.0),
                            _row("Org/Lic-GGUF", "l-q4_k_m.gguf", 1000, SHA["lic"], 1.0), _row("Org/Moved-GGUF", "m-q4_k_m.gguf", 1000, SHA["bad"], 1.0),
                            _row("Org/Orphan-GGUF", "mmproj-o-q8_0.gguf", 400, SHA["bad"], 8.0, "Q8_0"), _row("Intruder/Copy-GGUF", "c-q4_k_m.gguf", 1000, SHA["qwen"], 1.0)]}
    lock["pairs"] = [["rt", r["id"]] for r in lock["model_files"]]
    return FakeHub(repos, headers), lock


def _run(**kw):
    hub, lock = _world()
    hub.broken = kw.get("broken", set())
    return verify_lock(copy.deepcopy(lock), hub, RULES, approved_publishers=["Org"], checked_on="2026-10-30", workers=1), lock


def test_a_name_that_says_5b_is_replaced_by_the_tensor_count_and_the_disagreement_is_recorded():
    (new, report), _ = _run()
    small = next(r for r in new["model_files"] if r["repo"] == "Org/Small-0.5B-GGUF")
    assert small["params_b"] == 0.1 and small["architecture"] == "qwen2" and small["kind"] == "chat"  # 100000 x 1000 elements = 0.1B: the name said 5.0
    assert [c["field"] for c in small["conflicts"]] == ["params_b"] and small["conflicts"][0]["declared"] == 5.0 and small["conflicts"][0]["winner"] == "verified"
    assert {"id": small["id"], "change": "params_b", "was": 5.0, "now": 0.1} in report["changes"]


def test_a_projector_is_attached_to_the_vision_model_of_its_own_repository_and_is_not_a_row():
    (new, report), _ = _run()
    ids = {r["id"] for r in new["model_files"]}
    assert "Org/Vis-GGUF/mmproj-vl-q8_0.gguf" not in ids
    vl = next(r for r in new["model_files"] if r["repo"] == "Org/Vis-GGUF")
    assert vl["kind"] == "vision-language" and vl["requires_task"] == {} and any("includes vision" in x for x in vl["labels"]) and [c["file"] for c in vl["companions"]] == ["mmproj-vl-q8_0.gguf"] and vl["companions"][0]["sha256"] == SHA["proj"]
    assert report["companions"]["Org/Vis-GGUF@aaaaaaaa"] == ["mmproj-vl-q8_0.gguf"]


def test_a_projector_whose_model_is_not_in_the_catalog_is_dropped_with_its_reason():
    (new, _), _ = _run()
    why = [e for e in new["excluded"] if e["what"] == "Org/Orphan-GGUF/mmproj-o-q8_0.gguf"]
    assert why and why[0]["rule"] == "R1.1-projector" and "no model of its repository" in why[0]["reason"]


def test_a_checksum_the_listing_contradicts_and_a_publisher_nobody_approved_are_excluded_by_their_rules():
    (new, report), _ = _run()
    by_what = {e["what"]: e for e in new["excluded"]}
    assert by_what["Org/Moved-GGUF/m-q4_k_m.gguf"]["rule"] == "R6.1-checksum-matches-listing"
    assert by_what["Intruder/Copy-GGUF/c-q4_k_m.gguf"]["rule"] == "R6.2-publisher"
    assert {"Org/Moved-GGUF/m-q4_k_m.gguf", "Intruder/Copy-GGUF/c-q4_k_m.gguf"}.isdisjoint(r["id"] for r in new["model_files"])


def test_gated_and_unverified_licenses_stay_in_the_lock_but_are_not_offered_by_default():
    (new, _), _ = _run()
    rows = {r["repo"]: r for r in new["model_files"]}
    assert rows["Org/Gated-GGUF"]["offer_by_default"] is False and any("gated" in x for x in rows["Org/Gated-GGUF"]["labels"])
    assert rows["Org/Lic-GGUF"]["offer_by_default"] is False and "license unverified" in rows["Org/Lic-GGUF"]["labels"] and rows["Org/Lic-GGUF"]["license_header"] == "mit"
    assert rows["Org/Small-0.5B-GGUF"]["offer_by_default"] is True and rows["Org/Small-0.5B-GGUF"]["license_verified"] is True


def test_the_new_lock_is_consistent_counts_pairs_stamp_and_the_old_exclusions_are_kept():
    (new, report), old = _run()
    assert new["counts"]["model_files"] == len(new["model_files"]) and new["counts"]["excluded"] == len(new["excluded"]) and new["counts"]["combinations"] == len(new["pairs"])
    assert {p[1] for p in new["pairs"]} == {r["id"] for r in new["model_files"]}  # no pair points at a row that was dropped
    assert new["excluded"][0] == {"what": "old", "reason": "old"}
    assert new["verified"]["rules"] == rules_digest(RULES)[:16] and new["verified"]["files_checked"] == 8
    assert all(r["verified"]["label"] == "verified-by-code" and r["verified"]["revision"] == REV and r["min_ram_gib"] > 0 for r in new["model_files"])
    assert len(old["model_files"]) == 8 and "verified" not in old["model_files"][0]  # the input was not touched


def test_a_repository_the_hub_cannot_answer_for_is_a_problem_in_the_report_never_a_silent_pass():
    (new, report), _ = _run(broken={"Org/Lic-GGUF"})
    assert [p["what"] for p in report["problems"]] == ["Org/Lic-GGUF@aaaaaaaa"] and "HTTP 500" in report["problems"][0]["problem"]
    assert "Org/Lic-GGUF" not in {r["repo"] for r in new["model_files"]}  # nothing is claimed about a file that was not checked


def test_the_architecture_table_is_read_from_the_runtimes_own_source_at_the_pinned_commit():
    import httpx
    src = 'static const std::map<llm_arch, const char *> LLM_ARCH_NAMES = {\n    { LLM_ARCH_CLIP,             "clip"             }, // dummy\n    { LLM_ARCH_LLAMA,            "llama"            },\n    { LLM_ARCH_QWEN3VL,          "qwen3vl"          },\n};'
    seen = []

    def handler(req):
        seen.append(str(req.url))
        return httpx.Response(200, text=src) if req.url.host == "raw.githubusercontent.com" else httpx.Response(404)
    assets = [{"source": "llama.cpp", "tag": "b9", "commit": "c" * 40}, {"source": "llama.cpp", "tag": "b9", "commit": "c" * 40}, {"source": "other", "tag": "t", "commit": "d" * 40}]
    out = fetch_runtime_arches(assets, RULES, client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert out["llama.cpp"]["b9"]["architectures"] == ["clip", "llama", "qwen3vl"] and out["llama.cpp"]["b9"]["label"] == "verified-by-code"
    assert seen == [f"https://raw.githubusercontent.com/ggml-org/llama.cpp/{'c' * 40}/src/llama-arch.cpp"]  # one request for the one pinned commit; the other runtime is not guessed
    with pytest.raises(HubError, match="layout changed"):
        fetch_runtime_arches(assets[:1], RULES, client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="nothing here"))))


def test_checking_a_lock_twice_changes_nothing_and_never_erases_what_the_first_check_found():
    hub, lock = _world()
    once, _ = verify_lock(copy.deepcopy(lock), hub, RULES, approved_publishers=["Org"], checked_on="2026-10-30", workers=1)
    twice, report = verify_lock(copy.deepcopy(once), hub, RULES, approved_publishers=["Org"], checked_on="2026-10-30", workers=1)
    assert [r for r in report["changes"] if r["change"] in ("params_b", "bytes")] == []
    small = lambda lk: next(r for r in lk["model_files"] if r["repo"] == "Org/Small-0.5B-GGUF")  # noqa: E731
    assert small(twice)["conflicts"] == small(once)["conflicts"] and small(twice)["conflicts"][0]["declared"] == 5.0
    assert [r["id"] for r in twice["model_files"]] == [r["id"] for r in once["model_files"]]
    vl = lambda lk: next(r for r in lk["model_files"] if r["repo"] == "Org/Vis-GGUF")  # noqa: E731
    assert [c["file"] for c in vl(once)["companions"]] == ["mmproj-vl-q8_0.gguf"] and vl(twice)["companions"] == vl(once)["companions"]  # a second check does not lose the projectors the first attached


def _split_world(second_sha="7" * 64):
    from gguf_fixtures import U32
    first = gguf([kv("general.architecture", STR, "llama"), kv("general.license", STR, "apache-2.0"), kv("tokenizer.chat_template", STR, "t")], [("a", (1_000_000, 1000))])
    second = gguf([kv("general.architecture", STR, "llama")], [("b", (1_000_000, 3000))])
    repos = {"Org/Split-GGUF": {"info": _info(), "tree": {"s-q4_k_m-00001-of-00002.gguf": {"size": 600, "sha256": "7" * 64}, "s-q4_k_m-00002-of-00002.gguf": {"size": 900, "sha256": second_sha}}}}
    headers = {("Org/Split-GGUF", "s-q4_k_m-00001-of-00002.gguf"): first, ("Org/Split-GGUF", "s-q4_k_m-00002-of-00002.gguf"): second}
    row = _row("Org/Split-GGUF", "s-q4_k_m-00001-of-00002.gguf", 1500, "7" * 64, 7.0)
    row["shards"] = [{"file": "s-q4_k_m-00001-of-00002.gguf", "bytes": 600, "sha256": "7" * 64, "url": "u1"}, {"file": "s-q4_k_m-00002-of-00002.gguf", "bytes": 900, "sha256": "8" * 64, "url": "u2"}]
    return FakeHub(repos, headers), {"version": 1, "manifest_sha256": "x", "counts": {}, "runtime_assets": [], "excluded": [], "model_files": [row], "pairs": [["rt", row["id"]]]}


def test_a_split_model_is_verified_shard_by_shard_and_its_parameters_are_the_sum_of_all_shards():
    hub, lock = _split_world(second_sha="8" * 64)
    new, report = verify_lock(lock, hub, RULES, approved_publishers=["Org"], checked_on="2026-10-30", workers=1)
    [m] = new["model_files"]
    assert m["params_b"] == 4.0 and m["bytes"] == 1500 and len(m["shards"]) == 2  # 1.0B in the first shard + 3.0B in the second: the first shard alone would say 1.0
    assert report["problems"] == []


def test_a_shard_whose_checksum_the_listing_contradicts_excludes_the_whole_model():
    hub, lock = _split_world(second_sha="9" * 64)  # the listing says shard 2 has another checksum than the lock
    new, _ = verify_lock(lock, hub, RULES, approved_publishers=["Org"], checked_on="2026-10-30", workers=1)
    assert new["model_files"] == [] and new["excluded"][0]["rule"] == "R6.1-checksum-matches-listing"


def test_the_parameter_cap_is_judged_on_the_real_count_from_the_header_not_on_a_name():
    (new, report), _ = _run()
    assert [r for r in new["model_files"] if r["repo"] == "Org/Small-0.5B-GGUF"]  # named 5.0B, really 0.1B: under any cap
    hub, lock = _world()
    capped, _ = verify_lock(copy.deepcopy(lock), hub, RULES, approved_publishers=["Org"], checked_on="2026-10-30", workers=1, max_params_b=0.05)
    why = {e["what"]: e for e in capped["excluded"]}
    assert why["Org/Small-0.5B-GGUF/small-q4_k_m.gguf"]["rule"] == "manifest:max_model_params_b" and "read from the header" in why["Org/Small-0.5B-GGUF/small-q4_k_m.gguf"]["reason"]


def test_a_license_only_the_card_states_is_kept_and_labeled_declared_and_a_llama_license_is_recorded_by_name():
    hub, lock = _world()
    hub.repos["Org/Gated-GGUF"]["info"]["gated"] = False
    hub.repos["Org/Gated-GGUF"]["info"]["card_license"] = "llama3.1"
    hub.headers[("Org/Gated-GGUF", "g-q4_k_m.gguf")] = gguf([kv("general.architecture", STR, "llama"), kv("tokenizer.chat_template", STR, "t")], [("w", (4, 4))])  # the header states no license
    new, _ = verify_lock(lock, hub, RULES, approved_publishers=["Org"], checked_on="2026-10-30", workers=1)
    g = next(r for r in new["model_files"] if r["repo"] == "Org/Gated-GGUF")
    assert g["offer_by_default"] is True and g["license"] == "llama3.1" and g["license_name"] == "Llama 3.1 Community License" and g["license_class"] == "community-host-run"
    assert "declared-by-project" in g["license_basis"] and any("declared-by-project" in x for x in g["labels"]) and any("community license" in x for x in g["labels"])


WORKFLOW = """jobs:
  macos-cpu:
    strategy:
      matrix:
        include:
          - build: 'arm64'
            os: macos-26
            defines: "-DGGML_METAL_EMBED_LIBRARY=ON"
          - build: 'x64'
            os: macos-15-intel
            # Metal is disabled on x64
            defines: "-DGGML_METAL=OFF"
"""


def test_a_builds_backend_label_is_checked_against_the_runtimes_own_release_workflow_at_the_pinned_commit():
    import httpx
    from envrec.catalog.verify import check_backend_evidence
    seen = []

    def handler(req):
        seen.append(str(req.url))
        return httpx.Response(200, text=WORKFLOW)
    assets = [{"source": "llama.cpp", "tag": "b9", "commit": "c" * 40, "os": "macos", "arch": "arm64", "backend": "metal"}, {"source": "llama.cpp", "tag": "b9", "commit": "c" * 40, "os": "macos", "arch": "amd64", "backend": "cpu"}]
    ev = check_backend_evidence(assets, RULES, client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert [(e["os"], e["arch"], e["backend"], e["label"]) for e in ev] == [("macos", "arm64", "metal", "verified-by-code"), ("macos", "amd64", "cpu", "verified-by-code")]
    assert seen == [f"https://raw.githubusercontent.com/ggml-org/llama.cpp/{'c' * 40}/.github/workflows/release.yml"]
    wrong = [{**assets[0], "backend": "cpu"}, assets[1]]  # the catalog calls the Apple-silicon build a CPU build: the label no longer matches what the rules say it must be
    bad = check_backend_evidence(wrong, RULES, client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert bad[0]["label"] == "MISMATCH" and bad[0]["evidence_found"] and not bad[0]["label_in_catalog"]
    gone = check_backend_evidence(assets, RULES, client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="jobs: {}"))))
    assert all(e["label"] == "MISMATCH" and not e["evidence_found"] for e in gone)  # the workflow changed: nothing is claimed, the check says so

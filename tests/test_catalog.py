"""R3b catalog generator on a fixture matrix: exclusion reasons, computed combinations, approval gate, no hand-written entries."""

from __future__ import annotations

import pytest
import yaml

from catalog_fixtures import FakeMeta, H, manifest_raw, write_manifest
from envrec.catalog import Catalog, generate, write
from envrec.catalog.model import parse_params_b, parse_quant
from envrec.runtime.manifest import Manifest, NotApproved


@pytest.fixture
def gen(tmp_path):
    meta = FakeMeta()
    m = Manifest.load(write_manifest(tmp_path))
    return generate(m, meta), meta, m


def reasons(g):
    return {e["what"]: e["reason"] for e in g.lock.excluded}


def test_refuses_while_not_approved(tmp_path):
    meta = FakeMeta()
    with pytest.raises(NotApproved):
        generate(Manifest.load(write_manifest(tmp_path, approved=False)), meta)
    assert meta.calls == []  # not even a metadata read happened


def test_only_approved_sources_are_read(gen):
    _, meta, _ = gen
    urls = {c[1] for c in meta.calls if c[0] == "releases"}
    assert urls == {"https://example.test/llama.cpp", "https://example.test/rustlm"}
    assert {c[1] for c in meta.calls if c[0] == "model_repos"} == {"Qwen"}  # not somehub, not google


def test_runtime_assets_pinned_and_filtered(gen):
    g, _, _ = gen
    ids = {a.id for a in g.lock.runtime_assets}
    assert ids == {"llama.cpp:b200:linux/arm64:cpu", "rustlm:v1.0.0:linux/arm64:cpu"}  # rc tag and nightly ignored
    a = next(a for a in g.lock.runtime_assets if a.source == "llama.cpp")
    assert a.commit == "c" * 40 and a.sha256 == "a" * 64 and a.runnable is True
    assert next(a for a in g.lock.runtime_assets if a.source == "rustlm").runnable is False  # worker has no flag set for it
    r = reasons(g)
    assert "no checksum" in next(v for k, v in r.items() if "ubuntu-x64" in k)
    assert "artifact cap" in next(v for k, v in r.items() if "cuda" in k)


def test_model_filters_each_have_a_reason(gen):
    g, _, _ = gen
    kept = {m.id for m in g.lock.model_files}
    assert kept == {"Qwen/Qwen2.5-0.5B-Instruct-GGUF/qwen2.5-0.5b-instruct-q4_k_m.gguf",
                    "Qwen/Qwen2.5-0.5B-Instruct-GGUF/qwen2.5-0.5b-instruct-q8_0.gguf",
                    "Qwen/Qwen2.5-1.5B-Instruct-GGUF/qwen2.5-1.5b-instruct-q4_k_m.gguf",
                    "Qwen/Qwen2.5-1.5B-Instruct-GGUF/huge-1.5b-q8_0.gguf",  # 3.5 GB: a model has no size cap any more; whether it fits is decided per machine
                    "Qwen/Big-32B-GGUF/big-32b-q4_k_m.gguf"}  # a number in a name no longer excludes: the file's header sets the real count, and the manifest's cap is applied to that
    r = reasons(g)
    assert "allowlist" in r["Qwen/Research-3B-GGUF"]
    assert "no license" in r["Qwen/NoLicense-1B-GGUF"]
    assert "no checksum" in r["Qwen/Qwen2.5-1.5B-Instruct-GGUF/qwen2.5-1.5b-instruct-q8_0.gguf"]
    assert "incomplete" in r["Qwen/Big-32B-GGUF/big-32b-q4_k_m-00001-of-00004.gguf"]  # one shard of four is not a model
    assert not any("q2_k" in k for k in kept)  # quantization not in the manifest: silently ignored, not an exclusion
    assert not any("cap" in v for k, v in r.items() if "/" in k)  # no model is excluded by a cap


def test_a_complete_set_of_shards_is_one_entry_with_every_shard_listed(tmp_path):
    class Split(FakeMeta):
        def model_repos(self, author):
            return [{"repo": "Qwen/Split-7B-GGUF", "license": "apache-2.0"}, {"repo": "Qwen/Gone-7B-GGUF", "license": "apache-2.0"}]

        def repo_files(self, repo):
            if repo.startswith("Qwen/Split"):
                return {"revision": "w" * 40, "files": [{"path": f"split-7b-q4_k_m-0000{i}-of-00003.gguf", "bytes": 2_000_000_000 + i, "sha256": H(str(i))} for i in (1, 2, 3)]}
            return {"revision": "x" * 40, "files": [{"path": f"gone-7b-q4_k_m-0000{i}-of-00003.gguf", "bytes": 1_000_000_000, "sha256": H("a")} for i in (1, 3)]}
    g = generate(Manifest.load(write_manifest(tmp_path)), Split())
    [m] = g.lock.model_files
    assert m.id == "Qwen/Split-7B-GGUF/split-7b-q4_k_m-00001-of-00003.gguf" and m.bytes == 6_000_000_006 and m.params_b == 7.0
    assert [(s["file"][-19:], s["bytes"], s["sha256"]) for s in m.shards] == [(f"0000{i}-of-00003.gguf", 2_000_000_000 + i, H(str(i))) for i in (1, 2, 3)]
    assert sum(1 for e in g.lock.excluded if "incomplete" in e["reason"]) == 2  # the two shards of a set with one missing
    write(g, tmp_path / "l.yaml", tmp_path / "e.md")
    arts = Catalog.load(tmp_path / "l.yaml").artifacts()
    shard_arts = {k: a for k, a in arts.items() if "#shard" in k}
    assert sorted(shard_arts) == [f"{m.id}#shard{i}" for i in (1, 2, 3)] and m.id not in arts  # one downloadable artifact per shard, each with its own checksum and size
    assert [a.sha256 for _, a in sorted(shard_arts.items())] == [H(str(i)) for i in (1, 2, 3)]


def test_off_and_excluded_sources_are_listed(gen):
    g, _, _ = gen
    r = reasons(g)
    assert "propose-off" in r["source offrt"] and "exclude" in r["source badrt"] and "gemma" in r["source google"]
    assert "source hub" in r


def test_combinations_are_computed_from_the_matrix(gen):
    g, _, _ = gen
    # llama.cpp arm64 reads gguf -> all 5 model files; rustlm (gguf, safetensors) -> same 5; nothing is hand listed.
    assert len(g.lock.pairs) == 2 * 5
    assert g.lock.counts == {"runtime_assets": 2, "model_files": 5, "combinations": 10, "excluded": len(g.lock.excluded)}


def test_unreadable_format_pairs_not_made(tmp_path):
    def only_safetensors(raw):
        raw["runtime_sources"][0]["formats"] = ["safetensors"]
    meta = FakeMeta()
    g = generate(Manifest.load(write_manifest(tmp_path, mutate=only_safetensors)), meta)
    assert all(not rid.startswith("llama.cpp") for rid, _ in g.lock.pairs)


def test_lockfile_round_trip_and_artifacts(gen, tmp_path):
    g, _, _ = gen
    write(g, tmp_path / "catalog.lock.yaml", tmp_path / "catalog.excluded.md")
    cat = Catalog.load(tmp_path / "catalog.lock.yaml")
    assert len(cat) == 10
    e = next(iter(cat.entries()))
    assert e.min_disk_gb > (e.runtime.bytes + e.model.bytes) / 1e9
    arts = cat.artifacts()
    assert set(arts) == {a.id for a in g.lock.runtime_assets} | {m.id for m in g.lock.model_files}
    assert all(a.sha256 and a.url.startswith("https://") for a in arts.values())
    md = (tmp_path / "catalog.excluded.md").read_text()
    assert "Research-3B" in md and "allowlist" in md
    assert cat.get(e.id).id == e.id and cat.get("llama.cpp:b200:linux/arm64:cpu+nope") is None


def test_deterministic(tmp_path):
    m = Manifest.load(write_manifest(tmp_path))
    a, b = generate(m, FakeMeta()), generate(m, FakeMeta())
    assert a.lock.model_dump(mode="json") == b.lock.model_dump(mode="json")


def test_name_parsing():
    assert parse_params_b("Qwen2.5-1.5B-Instruct") == 1.5 and parse_params_b("qwen2.5-0.5b-instruct-q4_k_m.gguf") == 0.5
    assert parse_params_b("Qwen3-4B-Q4_K_M.gguf") == 4 and parse_params_b("no-size-here") is None
    assert parse_quant("Qwen3-4B-Q4_K_M.gguf") == "Q4_K_M" and parse_quant("x-q8_0.gguf") == "Q8_0" and parse_quant("model.gguf") is None


def test_real_manifest_source_statuses_exclude_the_right_things(tmp_path):
    """Run on a copy of the real manifest with the flag set in the COPY only, against fixture metadata for its ids."""
    import shutil
    from pathlib import Path
    src = Path(__file__).resolve().parents[1] / "config" / "runtime_manifest.yaml"
    raw = yaml.safe_load(src.read_text())
    raw["approved"] = True
    p = tmp_path / "m.yaml"
    p.write_text(yaml.safe_dump(raw))
    m = Manifest.load(p)
    calls = []

    class Empty(FakeMeta):
        def releases(self, repo_url):
            calls.append(repo_url)
            return []

        def model_repos(self, author):
            calls.append(author)
            return []
    g = generate(m, Empty())
    assert set(calls) == {"https://github.com/ggml-org/llama.cpp", "https://github.com/EricLBuehler/mistral.rs",
                          "Qwen", "HuggingFaceTB", "ibm-granite", "microsoft", "mistralai", "allenai", "TinyLlama", "bartowski", "google", "LiquidAI", "nvidia"}  # the last four are approved for NAMED repos only
    r = reasons(g)
    for off in ("llamafile", "ollama", "LocalAI", "ggml-org", "meta-llama"):  # google, liquidai and nvidia are approved for named repos now
        assert f"source {off}" in r


def test_cli_refuses_while_not_approved(tmp_path, capsys):
    from envrec.cli import main
    rc = main(["catalog", "--manifest", str(write_manifest(tmp_path, approved=False)), "--lock", str(tmp_path / "l.yaml"),
               "--excluded", str(tmp_path / "e.md")])
    assert rc == 2 and not (tmp_path / "l.yaml").exists()
    assert "approved: false" in capsys.readouterr().err


def test_live_client_only_talks_to_allowed_hosts():
    import httpx
    from envrec.catalog.live import HostNotAllowed, LiveMetadata
    seen = []

    def handler(req):
        seen.append(req.url.host)
        return httpx.Response(200, json=[])
    lm = LiveMetadata(httpx.Client(transport=httpx.MockTransport(handler)), github_token="t")
    assert lm.releases("https://github.com/ggml-org/llama.cpp") == [] and seen == ["api.github.com"]
    with pytest.raises(HostNotAllowed):
        lm._get("https://evil.example/x")
    assert seen == ["api.github.com"]


def test_live_client_parses_github_and_hf_shapes():
    import httpx
    from envrec.catalog.live import LiveMetadata

    def handler(req):
        u = str(req.url)
        if "releases" in u:
            return httpx.Response(200, json=[{"tag_name": "b1", "target_commitish": "a" * 40, "prerelease": True, "assets": [
                {"name": "x.tar.gz", "browser_download_url": "https://github.com/x", "size": 5, "digest": "sha256:" + "b" * 64}]}]
                if "page=1" in u else [])
        if u.endswith("/tree/" + "s" * 40):
            return httpx.Response(200, json=[{"type": "file", "path": "m-q4_k_m.gguf", "lfs": {"size": 10, "oid": "c" * 64}},
                                             {"type": "file", "path": "README.md", "size": 3}])
        if "/api/models/Qwen/M" in u:
            return httpx.Response(200, json={"sha": "s" * 40, "tags": ["gguf", "license:apache-2.0"]})
        return httpx.Response(200, json=[{"id": "Qwen/M"}])
    lm = LiveMetadata(httpx.Client(transport=httpx.MockTransport(handler)))
    rel = lm.releases("https://github.com/o/r")
    assert rel[0]["assets"][0]["sha256"] == "b" * 64 and rel[0]["commit"] == "a" * 40
    assert lm.model_repos("Qwen") == [{"repo": "Qwen/M", "license": "apache-2.0"}]
    f = lm.repo_files("Qwen/M")
    assert f["revision"] == "s" * 40 and f["files"][0] == {"path": "m-q4_k_m.gguf", "bytes": 10, "sha256": "c" * 64}
    assert f["files"][1]["sha256"] is None


def test_a_rate_limit_is_waited_out_and_a_cached_answer_is_not_asked_again(tmp_path, monkeypatch):
    import httpx
    from envrec.catalog import live
    from envrec.catalog.live import LiveMetadata
    monkeypatch.setattr(live.time, "sleep", lambda s: None)
    calls = []

    def handler(req):
        calls.append(str(req.url))
        return httpx.Response(429, headers={"retry-after": "1"}) if len(calls) == 1 else httpx.Response(200, json={"sha": "a" * 40})
    lm = LiveMetadata(httpx.Client(transport=httpx.MockTransport(handler)), cache_dir=tmp_path / "c", pace_s=0.1)
    assert lm._get("https://huggingface.co/api/models/o/r") == {"sha": "a" * 40} and len(calls) == 2  # one 429, then the answer
    assert lm._get("https://huggingface.co/api/models/o/r") == {"sha": "a" * 40} and len(calls) == 2  # from the cache: nothing asked
    flaky = iter([504, 200])
    lm2 = LiveMetadata(httpx.Client(transport=httpx.MockTransport(lambda r: (lambda c: httpx.Response(c, json={"ok": 1}))(next(flaky)))))
    assert lm2._get("https://api.github.com/repos/o/r/releases") == {"ok": 1}  # a gateway timeout is waited out too
    always = LiveMetadata(httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429))), max_retries=2)
    with pytest.raises(httpx.HTTPStatusError):
        always._get("https://huggingface.co/api/models/o/r2")  # it gives up after the retries and says why, never returns nothing


def test_a_quantization_published_both_as_one_file_and_as_shards_is_listed_once(tmp_path):
    class Both(FakeMeta):
        def model_repos(self, author):
            return [{"repo": "Qwen/Both-7B-GGUF", "license": "apache-2.0"}]

        def repo_files(self, repo):
            return {"revision": "y" * 40, "files": [{"path": "both-7b-q4_k_m.gguf", "bytes": 4_000_000_000, "sha256": H("1")}] + [{"path": f"both-7b-q4_k_m-0000{i}-of-00002.gguf", "bytes": 2_000_000_000, "sha256": H(str(i + 1))} for i in (1, 2)]
                    + [{"path": "both-7b-q8_0-00001-of-00002.gguf", "bytes": 4_000_000_000, "sha256": H("5")}, {"path": "both-7b-q8_0-00002-of-00002.gguf", "bytes": 4_000_000_000, "sha256": H("6")}]}
    g = generate(Manifest.load(write_manifest(tmp_path)), Both())
    assert [(m.file, bool(m.shards)) for m in g.lock.model_files] == [("both-7b-q4_k_m.gguf", False), ("both-7b-q8_0-00001-of-00002.gguf", True)]  # the Q8_0 exists only split: kept, as one entry
    assert any("also published as a single file" in e["reason"] for e in g.lock.excluded)


def test_a_source_that_names_its_repos_is_approved_for_those_repos_only(tmp_path):
    """bartowski is approved for TWO repos (the ungated Meta Llama 3.1 8B Instruct and Llama 3.2 3B Instruct quantizations): its other repos are never listed, read or offered."""
    from pathlib import Path
    raw = yaml.safe_load((Path(__file__).resolve().parents[1] / "config" / "runtime_manifest.yaml").read_text())
    raw["approved"] = True
    src = next(x for x in raw["model_sources"] if x["id"] == "bartowski")
    assert src["status"] == "approve" and src["repos"] == ["bartowski/Meta-Llama-3.1-8B-Instruct-GGUF", "bartowski/Llama-3.2-3B-Instruct-GGUF"]
    raw["runtime_sources"] = []
    raw["model_sources"] = [src]
    p = tmp_path / "m.yaml"
    p.write_text(yaml.safe_dump(raw))
    asked = []

    class Meta(FakeMeta):
        def model_repos(self, author):
            return [{"repo": "bartowski/Meta-Llama-3.1-8B-Instruct-GGUF", "license": "llama3.1"}, {"repo": "bartowski/Some-Other-Model-GGUF", "license": "llama3.1"}]

        def repo_files(self, repo):
            asked.append(repo)
            return {"revision": "r" * 40, "license": "llama3.1", "files": [{"path": "m-Q4_K_M.gguf", "bytes": 5, "sha256": "a" * 64}]}
    g = generate(Manifest.load(p), Meta())
    assert asked == ["bartowski/Meta-Llama-3.1-8B-Instruct-GGUF"] and [m.repo for m in g.lock.model_files] == ["bartowski/Meta-Llama-3.1-8B-Instruct-GGUF"]

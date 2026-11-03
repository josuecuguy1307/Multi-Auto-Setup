"""V5: public evidence from public sources, by code. One generic extractor reads a data row; a result enters only while its number is in its stored capture; vendor-reported results and aggregators are
dropped; the explain line carries the tier, the source and its date, the honest model-only label and an age warning. SIMULATED-LOCAL for the extractor (fake pages); the shipped entries were fetched for REAL
(scripts/build_evidence.py) and are re-verified here against their stored captures."""
from __future__ import annotations

import json
import shutil
import time

import yaml

from envrec_coordinator import evidence_sources as ES
from envrec_coordinator.router import evidence as EV
from envrec_coordinator.router.model import Route

REAL = ES.load_sources()
SPEC = {**REAL, "sources": [REAL["sources"][0]], "left_out": []}  # only the Hub row: the fake pages are about a fake model whose base model the fake lock names
LOCK = {"model_files": [{"file": "m-Q4.gguf", "base_model": "org/model-x"}, {"file": "m-Q8.gguf", "base_model": "org/model-x"}]}
HUB = "org/model-x"


def model_page(rows):
    return json.dumps({"evalResults": [{"verified": v, "data": {"dataset": {"id": ds, "task_id": t}, "value": val, "date": "2026-06-02", "source": {"name": n, "url": u}}} for ds, t, val, n, u, v in rows]}).encode()


ROWS = [("LEXam-Benchmark/LEXam", "mcq", 24.04, "LEXam Leaderboard", "https://lexam-benchmark.github.io/", False),
        ("org-a/bench-a", "gsm8k", 84.5, "Model Card", f"https://huggingface.co/{HUB}", False),
        ("TIGER-Lab/MMLU-Pro", "all", 36.6, "EvalEval", "https://huggingface.co/datasets/evaleval/EEE_datastore/blob/x.json", False),
        ("some/unlisted", "t", 1.0, "A site", "https://example.org/", False),
        ("thamilvendhan/signalbench", "src", 0.63, "bench", "https://huggingface.co/datasets/thamilvendhan/signalbench", False)]


def fake(cap_official=("benchmark:official",)):
    asked = []

    def fetch(url):
        asked.append(url)
        if "/models/" in url:
            return True, model_page(ROWS), ""
        tags = list(cap_official) if "LEXam" in url else ["llm"]
        return True, json.dumps({"id": "x", "tags": tags}).encode(), ""
    return fetch, asked


def built(tmp_path, **kw):
    fetch, asked = fake(**kw)
    return ES.build(SPEC, LOCK, fetch=fetch, today="2026-11-02", captures=tmp_path / "caps"), asked


def test_the_extractor_drops_vendor_reports_and_aggregators_and_builds_only_for_described_benchmarks(tmp_path):
    out, asked = built(tmp_path)
    assert sorted(e["dataset"] for e in out["entries"]) == ["LEXam-Benchmark/LEXam", "thamilvendhan/signalbench"]
    assert out["dropped"] == {"vendor-reported: the model's own card": 1, "an aggregator, not a benchmark": 1, "no benchmark described in the data: some/unlisted": 1}
    lex = next(e for e in out["entries"] if e["dataset"].startswith("LEXam"))
    assert lex["applies"] == {"model": HUB, "models": ["m-Q4.gguf", "m-Q8.gguf"]} and lex["source_tier"] == "2" and lex["official_benchmark"] and lex["value"] == 24.04 and lex["source_date"] == "2026-06-02"
    assert next(e for e in out["entries"] if "signalbench" in e["dataset"])["source_tier"] == "3"  # not an official benchmark: shown, ranked last
    assert asked[0].startswith("https://huggingface.co/api/models/org/model-x") and len(asked) == 3  # the model page and the two benchmark cards: the audit of what left


def test_a_number_counts_only_while_it_is_in_its_capture(tmp_path):
    out, _ = built(tmp_path)
    caps = tmp_path / "caps"
    e = next(x for x in out["entries"] if x["dataset"].startswith("LEXam"))
    assert ES.verify(e, SPEC, caps) == (True, "")
    assert ES.verify({**e, "value": 99.0}, SPEC, caps) == (False, "the number is not in the capture")  # a value the model or a person typed is not in the page
    assert ES.verify({**e, "source_url": "https://example.org/other"}, SPEC, caps)[0] is False
    (caps / f"{e['capture_sha256']}.json").write_bytes(b"{}")
    assert ES.verify(e, SPEC, caps) == (False, "the stored page does not match its hash")
    shutil.rmtree(caps)
    assert ES.verify(e, SPEC, caps) == (False, "the capture is not stored")


def test_a_page_that_cannot_be_read_or_a_model_with_no_catalog_file_is_skipped_with_the_reason(tmp_path):
    out = ES.build(SPEC, LOCK, fetch=lambda u: (False, b"", "HTTP 404"), today="2026-11-02", captures=tmp_path)
    assert out["entries"] == [] and "could not be read" in out["skipped"][0]["reason"]
    out2 = ES.build(SPEC, {"model_files": []}, fetch=lambda u: (True, b"{}", ""), today="2026-11-02", captures=tmp_path)
    assert out2["entries"] == [] and out2["models_used"] == []  # no catalog model, nothing to look up


def doc_with(entry, **top):
    return {"entries": [entry], "excluded_benchmarks": [], **top}


def test_the_explain_line_has_tier_source_date_the_honest_label_and_an_age_warning(tmp_path):
    out, _ = built(tmp_path)
    lex = next(x for x in out["entries"] if x["dataset"].startswith("LEXam"))
    route = Route(environment_id="llamacpp-local", subtype="local_model", model_id="m-Q4.gguf", access_id="local-hardware", machine_id="mac")
    now = time.mktime((2026, 11, 2, 12, 0, 0, 0, 0, 0))
    d = EV.describe(route, "text", ["legal"], None, None, doc_with(lex, warn_after_days=365), now)
    assert d["tier"] == "public_model_only" and d["date"] == "2026-06-02" and d["warning"] == ""
    assert d["text"].startswith("evidence: public for the model only, full precision; the quantization and the harness are not measured here (source tier 2") and d["text"].endswith(", LEXam Leaderboard (listed on the Hugging Face Hub for org/model-x), 2026-06-02")
    old = EV.describe(route, "text", ["legal"], None, None, doc_with(lex, warn_after_days=30), now)
    assert "Warning: this result was published on 2026-06-02, 153 days ago" in old["text"]
    other = Route(environment_id="llamacpp-local", subtype="local_model", model_id="another.gguf", access_id="local-hardware", machine_id="mac")
    assert EV.describe(other, "text", ["legal"], None, None, doc_with(lex), now)["tier"] == "none"  # a model the entry does not name gets nothing


def test_the_shipped_entries_are_still_in_their_stored_captures_and_name_only_catalog_models():
    doc = EV.load()
    assert len(doc["entries"]) >= 20
    for e in doc["entries"]:
        assert ES.verify(e, REAL) == (True, ""), e["id"]
        assert e["source_tier"] in ("1", "2", "3") and e["source_url"].startswith("https://") and e["retrieved"] and e["source_date"]
    lock = yaml.safe_load((ES.SKILLS_DIR.parent / "catalog.lock.yaml").read_text(encoding="utf-8"))
    files = {m["file"] for m in lock["model_files"]}
    assert all(e["applies"]["models"] and set(e["applies"]["models"]) <= files for e in doc["entries"])  # the route ids are real catalog files
    assert not any(w in e["hub_id"].lower() for e in doc["entries"] for w in REAL["not_for_evidence"])  # models used only to build or test the project get none
    assert {c["source"] for c in doc["coverage"]} >= {"bfcl_overall", "livecodebench_generation", "SWE-rebench"}
    lcb = next(c for c in doc["coverage"] if c["source"] == "livecodebench_generation")
    assert lcb["catalog_models_matched"] == [] and lcb["rows"] > 20000 and len(lcb["capture_sha256"]) == 64 and lcb["models_in_capture"]  # the capture shows it holds no catalog model
    assert {c["capture_sha256"] for c in doc["coverage"] if c.get("kind") == "unavailable" and c["source"] == "SWE-rebench"} <= {p.stem for p in ES.CAPTURES_DIR.glob("*.json")}


def test_a_csv_leaderboard_and_a_per_problem_file_are_read_by_the_same_extractor(tmp_path):
    csv_body = b"Rank,Overall Acc,Model,Model Link\n1,50.50%,Big-Model,x\n2,25.83%,Small-Model (Prompt),y\n3,not a number,Other,z\n"
    lcb = json.dumps({"performances": [{"model": "M1", "pass@1": 40.0}, {"model": "M1", "pass@1": 60.0}, {"model": "M2", "pass@1": 10.0}], "models": [{"model_repr": "M1", "link": "https://huggingface.co/org/model-x"}, {"model_repr": "M2", "link": "https://huggingface.co/org/other"}]}).encode()
    spec = {**SPEC, "sources": [dict(REAL["sources"][1], names={"Small-Model (Prompt)": "org/model-x", "Big-Model": "org/not-in-catalog"}, url="https://t/csv", date_from=None, date="2026-04-12"),
                                dict(REAL["sources"][2], url="https://t/json", date="2026-05-01")], "benchmarks": REAL["benchmarks"]}
    asked = []

    def fetch(url, max_bytes=None):
        asked.append((url, max_bytes))
        return True, csv_body if url.endswith("csv") else lcb, ""
    out = ES.build(spec, LOCK, fetch=fetch, today="2026-11-02", captures=tmp_path / "caps")
    by = {e["dataset"]: e for e in out["entries"]}
    assert by["bfcl"]["value"] == 25.83 and by["bfcl"]["source_tier"] == "2" and by["bfcl"]["source_date"] == "2026-04-12" and by["bfcl"]["applies"]["models"] == ["m-Q4.gguf", "m-Q8.gguf"]
    assert by["livecodebench"]["value"] == 50.0 and "mean of 2 problems" in by["livecodebench"]["note"]  # the code averaged the problems of the model whose link is the catalog's
    assert asked == [("https://t/csv", 2000000), ("https://t/json", 12000000)]  # a row asks for ITS cap
    assert [c["catalog_models_matched"] for c in out["coverage"]] == [["org/model-x"], ["org/model-x"]] and out["coverage"][0]["rows"] == 3
    for e in out["entries"]:
        assert ES.verify(e, spec, tmp_path / "caps") == (True, "")


def test_a_row_cap_is_asked_for_and_kept_under_the_datas_ceiling(monkeypatch):
    import envrec_coordinator.research as R
    seen = {}

    class FakeCollector:
        def http_get(self, name, url, timeout_s, max_bytes=None):
            seen["max"] = max_bytes
            from envrec.discovery.collectors import Capture
            return Capture(name, url, b"{}", 0, "local", "x")
    monkeypatch.setattr("envrec.discovery.collectors.LocalCollector", FakeCollector)
    assert R.real_fetch("https://x/y", 5000000)[0] is True and seen["max"] == 5000000
    from envrec.discovery.docsources import load_sources
    assert 12000000 <= load_sources().ceiling_bytes and "gorilla.cs.berkeley.edu" in load_sources().hosts


def test_models_used_only_to_build_or_test_the_project_are_skipped_and_listed(tmp_path):
    lock = {"model_files": [{"file": "a.gguf", "base_model": "org/model-x"}, {"file": "b.gguf", "base_model": "claude/Some-Model"}]}
    out = ES.build(SPEC, lock, fetch=lambda u, m=None: (True, model_page(ROWS), ""), today="2026-11-02", captures=tmp_path)
    assert out["models_skipped"] == ["claude/Some-Model"] and out["models_used"] == ["org/model-x"] and not any("claude" in q for q in out["requests"])


def test_the_collector_never_reads_more_than_the_datas_ceiling_whatever_a_row_asks_for(monkeypatch):
    import urllib.request

    from envrec.discovery.collectors import LocalCollector
    from envrec.discovery.docsources import load_sources
    reads = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            reads.append(n)
            return b"{}"

    class Opener:
        def open(self, req, timeout=None):
            return Resp()
    monkeypatch.setattr(urllib.request, "build_opener", lambda *h: Opener())
    src = load_sources()
    for ask, want in ((None, src.max_bytes), (5000, 5000), (10 ** 9, src.ceiling_bytes)):
        LocalCollector().http_get("t", "https://api.github.com/x", 5.0, ask)
        assert reads[-1] == want + 1, ask

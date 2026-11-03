"""P3: the PyPI and npm readers against REAL payloads (tests/fixtures/registries, byte for byte from pypi.org and registry.npmjs.org on 2026-11-02, hashes in MANIFEST.json). What the real payloads taught: a PyPI page
often states its license only in `license` text or only as a classifier; npm's full package document is tens of MB (over the page cap), so the reader uses the registry's `/latest` manifest."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from test_research import refused

from envrec_coordinator import research as RS
from envrec_net import envrun as E

F = Path(__file__).parent / "fixtures" / "registries"
MANIFEST = json.loads((F / "MANIFEST.json").read_text())
BY_URL = {v["url"]: k for k, v in MANIFEST["files"].items()}


def researcher(tmp_path, tags=("coding",)):
    return RS.Researcher(dir=tmp_path / "researched", tags=list(tags), sensitivity="public", fetch=lambda u: (True, (F / BY_URL[u]).read_bytes(), "") if u in BY_URL else (False, b"", "HTTP 404"), today="2026-11-02",
                         hosts={"pypi.org", "registry.npmjs.org"})


def test_the_stored_captures_are_the_bytes_the_manifest_names():
    for name, v in MANIFEST["files"].items():
        b = (F / name).read_bytes()
        assert len(b) == v["bytes"] and hashlib.sha256(b).hexdigest() == v["sha256"] and v["url"].startswith("https://")


@pytest.mark.parametrize("url,license_,basis", [
    ("https://pypi.org/pypi/ollama/json", "MIT", "info.license_expression"),
    ("https://pypi.org/pypi/files-to-prompt/json", "Apache-2.0", "info.license"),
    ("https://pypi.org/pypi/aider-chat/json", None, None),  # only 'License :: OSI Approved :: Apache Software License': a family, not an identifier
    ("https://registry.npmjs.org/opencode-ai/latest", "MIT", "license"),
    ("https://registry.npmjs.org/%40openai%2Fcodex/latest", "Apache-2.0", "license"),
    ("https://registry.npmjs.org/%40earendil-works%2Fpi-coding-agent/latest", "MIT", "license"),
])
def test_each_real_payload_gives_its_license_from_where_it_states_it_or_none(tmp_path, url, license_, basis):
    d = researcher(tmp_path).fetch_source(url)
    (item,) = d["items"]
    assert item["license"] == license_ and item.get("license_basis") == basis and item["id"] and d["kind"] in ("pypi", "npm")
    assert item["checks"] == ([] if license_ else ["license_missing"])  # no star count or push date on a package page: those checks do not apply


def test_a_package_that_states_its_license_is_recorded_as_a_hypothesis_and_one_that_does_not_is_refused_by_name(tmp_path):
    r = researcher(tmp_path)
    codex = r.fetch_source("https://registry.npmjs.org/%40openai%2Fcodex/latest")
    got = r.record_candidate(codex["capture_ref"], 1, "provider", "A coding agent CLI.")
    assert got["license"] == "Apache-2.0" and got["evidence_state"] == "hypothesis"
    envs, rejected = E.load_catalog(extra_dir=tmp_path / "researched")
    assert rejected == {} and not E.is_runnable(envs[got["id"]]) and envs[got["id"]]["fetched"]["capture_sha256"] == codex["sha256"]
    aider = r.fetch_source("https://pypi.org/pypi/aider-chat/json")
    assert refused(r.record_candidate, aider["capture_ref"], 1, "own_harness", "x").code == "license_missing"


def test_the_full_npm_package_document_is_not_a_page_the_reader_asks_for(tmp_path):
    assert refused(researcher(tmp_path).check_url, "https://registry.npmjs.org/opencode-ai").code == "page_kind_unknown"  # 25,068,536 bytes in the real registry: over the cap, and not needed
    assert refused(researcher(tmp_path).check_url, "https://registry.npmjs.org/%40openai%2Fcodex").code == "page_kind_unknown"


def test_a_domain_word_that_the_description_lacks_is_rejected_for_a_package(tmp_path):
    r = researcher(tmp_path, tags=("astronomy",))
    d = r.fetch_source("https://registry.npmjs.org/opencode-ai/latest")
    assert refused(r.record_candidate, d["capture_ref"], 1, "own_harness", "x").code == "no_domain_in_description"


def test_free_text_in_the_license_field_is_not_an_identifier(tmp_path):
    body = json.dumps({"info": {"name": "x", "summary": "coding tool", "license": "See the LICENSE file in the repository", "classifiers": [], "package_url": "https://pypi.org/project/x/", "version": "1"}}).encode()
    r = RS.Researcher(dir=tmp_path / "r", tags=["coding"], sensitivity="public", fetch=lambda u: (True, body, ""), today="2026-11-02", hosts={"pypi.org"})
    (item,) = r.fetch_source("https://pypi.org/pypi/x/json")["items"]
    assert item["license"] is None and item["checks"] == ["license_missing"]

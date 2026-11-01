"""The name-based detectors against repositories OUTSIDE the catalog (REAL facts, saved by scripts/catalog_detector_probe.py). Each repository was reviewed against signals that do not come from its name; every false
positive the first run found is here with the kind it must have now."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from envrec.catalog import rules as R

RULES = R.Rules.load()
DOC = json.loads((Path(__file__).parent / "fixtures" / "detector_probe" / "outside_repos.json").read_text())


@pytest.mark.parametrize("entry", DOC["repos"], ids=[e["repo"] for e in DOC["repos"]])
def test_a_repository_outside_the_catalog_gets_the_kind_its_independent_signals_say(entry):
    row = R.derive_row({**entry["facts"], "file.bytes": 1, "file.revision": "a" * 40, "file.sha256": "b" * 64, "hub.listing_sha256": "b" * 64, "hub.listing_size": 1}, RULES)
    assert row["kind"] == entry["expected_kind"], (entry["why"], row["rules_fired"])
    if entry["expected_kind"] not in ("chat", "vision-language"):  # a vision model is offered, labeled 'includes vision'; the others are gated
        assert not row["offer_by_default"] or row["requires_task"], "a model that is not a chat model must be gated by a task"  # it is never offered for plain chat


def test_the_false_positives_of_the_first_run_stay_fixed():
    kinds = {e["repo"]: R.derive_row({**e["facts"]}, RULES)["kind"] for e in DOC["repos"]}
    assert kinds["JetBrains/Mellum2.1-12B-A2.5B-Thinking-GGUF"] != "draft" and kinds["peculiar-ragdoll/Tiel-Coder-35B-A3B-GGUF-MTP"] != "draft"  # a tag that says "supports speculative decoding" does not make a draft
    assert kinds["ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF"] == "reranker" and kinds["handy-computer/whisper-large-v3-gguf"] != "base" and kinds["Abiray/Shieldstral-1.0-3B-GGUF"] == "guard"

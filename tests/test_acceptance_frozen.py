"""The router acceptance examples are frozen: the file's hash is recorded, and a change to the file fails here (changes go in ROUTER_ACCEPTANCE_AMENDMENTS.md with the owner's words)."""

import hashlib
from pathlib import Path

DOCS = Path(__file__).resolve().parents[1] / "docs"


def test_acceptance_examples_are_unchanged():
    recorded = (DOCS / "ROUTER_ACCEPTANCE.sha256").read_text().strip()
    actual = hashlib.sha256((DOCS / "ROUTER_ACCEPTANCE.md").read_bytes()).hexdigest()
    assert actual == recorded, "docs/ROUTER_ACCEPTANCE.md changed after it was frozen; record an amendment instead of editing it"

"""P1-DOCS: the decisions and acceptance documents carry what MISSION.md Phase 1 requires, and were reviewed."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("heading", [
    "D-003 Architecture", "D-004 Approval token", "D-005 Threat model", "D-006 Guardrails", "D-007 Key MCU", "D-008 Display",
    "D-009 PCB as code", "D-010 \"No trace\"", "D-002 Harness and Claude Code features adopted",
])
def test_decision_present(heading):
    assert f"## {heading}" in read("DECISIONS.md")


def test_advisor_advice_recorded_verbatim_with_model_name():
    d = read("DECISIONS.md")
    assert "## A-1 Advisor review — Fable 5.2" in d
    for section in range(1, 9):
        assert f"**{section}." in d.split("## A-1", 1)[1]


def test_docs_reviewed_by_a_fresh_context_reviewer():
    d = read("DECISIONS.md")
    m = re.search(r"## R-1 Reviewer pass .*?\n(.*?)(?=\n## |\Z)", d, re.S)
    assert m and len(m.group(1).strip()) > 400, "the reviewer's verdict on DECISIONS.md and ACCEPTANCE.md must be recorded"


def test_every_guardrail_maps_to_a_criterion():
    table = read("DECISIONS.md").split("## D-006", 1)[1].split("## D-007", 1)[0]
    rows = [l for l in table.splitlines() if l.startswith("| ") and re.match(r"\| \d+ ", l)]
    assert [int(r.split()[1]) for r in rows] == list(range(1, 11))
    assert all(re.search(r"P\d-[A-Z-]+", r) for r in rows)


def test_acceptance_has_every_phase_gate_and_a_generated_table():
    a = read("ACCEPTANCE.md")
    for n in range(1, 7):
        assert f"**Phase {n}.**" in a
    assert "<!-- BEGIN GENERATED: device_accept.py -->" in a and "<!-- END GENERATED: device_accept.py -->" in a


def test_third_party_has_a_device_section():
    assert "## MAS portable device" in read("THIRD_PARTY.md")


def test_needs_human_covers_the_mission_items():
    n = read("NEEDS_HUMAN.md").lower()
    for word in ("physical machines", "buying boards", "review of the pcb", "arxiv", "license"):
        assert word in n

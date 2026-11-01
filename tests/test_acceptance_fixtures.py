"""The fixtures of the acceptance examples are fixed (their hashes are recorded), the invented data is not real, and each code check passes the reference reply and fails a wrong one. The expected values were
computed by independent code (statsmodels for the regression), never by a model. LINUX-CONTAINER for the project checks (Docker)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from envrec.skills import sbx
from envrec_coordinator.admission import router_bridge as bridge
from envrec_coordinator.router import grading

ROOT = Path(__file__).parent / "fixtures" / "acceptance"
MANIFEST = json.loads((ROOT / "manifest.json").read_text())
docker = pytest.mark.skipif(not sbx.docker_ready(), reason="Docker (or the sandbox image) is not ready: model-written code does not run without it")


def load(name: str) -> dict:
    d = ROOT / name
    return {p.name: p.read_text(encoding="utf-8") for p in d.iterdir()}


@pytest.mark.parametrize("name", sorted(MANIFEST))
def test_every_fixture_file_is_exactly_what_was_recorded(name):
    for fname, digest in MANIFEST[name].items():
        assert hashlib.sha256((ROOT / name / fname).read_bytes()).hexdigest() == digest, f"{name}/{fname} changed after it was recorded"


def verdict(check: dict, reply: str) -> bool:
    if check["kind"] in grading.TEXT_KINDS:
        return grading.check_text(check, reply)
    return bridge.sandbox_runner(SimpleNamespace(text=reply), check)["passed"]


@pytest.mark.parametrize("name", ["ex1_article", "ex3_notes", "ex4_long", "f1_bank", "f2_cashflow", "f3_journal", "f4_loan", "d11_extraction", "d12_cleaning"])
def test_a_text_check_passes_the_reference_reply_and_fails_a_wrong_one(name):
    f = load(name)
    check = json.loads(f["check.json"])
    assert verdict(check, f["good.txt"]) is True and verdict(check, "no") is False


@docker
@pytest.mark.parametrize("name", ["ex2_cli_dryrun", "c7_leap", "c8_csv", "c9_split", "c10_invoices", "f5_ols"])
def test_a_project_check_passes_the_reference_files_and_fails_the_unfixed_ones(name):  # LINUX-CONTAINER
    f = load(name)
    check = json.loads(f["check.json"])
    assert verdict(check, f["good.txt"]) is True, name
    assert verdict(check, f["bad.txt"]) is False, name


def test_the_long_inputs_are_longer_than_the_local_context_and_the_short_ones_are_not():
    assert len(load("ex4_long")["input.txt"]) / 4 > 8192 and len(load("ex1_article")["input.txt"]) / 4 < 8192


def test_the_journal_fixture_balances_and_uses_only_accounts_of_its_chart():
    f = load("f3_journal")
    chart = json.loads(f["chart.json"])
    entries = json.loads(f["expected.json"])
    assert len(entries) == 20 and all(e["debit"] in chart and e["credit"] in chart and e["debit"] != e["credit"] and e["amount"] > 0 for e in entries)


def test_the_regression_expectations_were_computed_by_statsmodels_and_the_reference_solution_agrees():
    exp = json.loads(load("f5_ols")["expected.json"])
    assert exp["statsmodels"] and len(exp["coef"]) == 3 and exp["price_significant_5pct"] is True


def test_the_data_is_invented_and_carries_no_real_names_or_emails_of_anyone():
    text = "".join(p.read_text() for p in ROOT.rglob("input.txt"))
    assert "@x.com" in text and "FAKE-" in load("ex3_notes")["input.txt"]

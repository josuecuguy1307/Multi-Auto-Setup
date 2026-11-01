"""The rules of `model-catalog-rules` as data: the engine, and one fixture per rule that must FAIL when that rule is removed (docs/HARNESS_SPEC.md: knowledge lives in skills and data, code is the guard)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from catalog_rules_support import matches, run
from envrec.catalog import rules as R

FIXTURES = Path(__file__).parent / "fixtures" / "catalog_rules"
RULES = R.Rules.load()
SKILL = Path(__file__).resolve().parent.parent / "skills" / "model-catalog-rules" / "SKILL.md"
CASES = [(f.name, json.loads(f.read_text()), c) for f in sorted(FIXTURES.glob("*.json")) for c in json.loads(f.read_text())["cases"]]


def test_every_rule_has_a_fixture_and_every_fixture_names_a_rule():
    named = {json.loads(f.read_text())["rule"] for f in FIXTURES.glob("*.json")}
    assert sorted(set(RULES.ids()) - named) == []  # a rule nobody tests
    assert sorted(named - set(RULES.ids())) == []  # a fixture for a rule that is gone
    assert len(RULES.ids()) == len(set(RULES.ids()))


@pytest.mark.parametrize("fname,spec,case", CASES, ids=[f"{n[:-5]}::{c['name'][:40]}" for n, _s, c in CASES])
def test_the_fixture_holds_with_the_rule_and_fails_without_it(fname, spec, case):
    with_rule = matches(case["expect"], run(RULES, spec["kind"], case))
    assert with_rule == [], f"{fname}: with every rule the expectation must hold: {with_rule}"
    without = matches(case["expect"], run(RULES.without(spec["rule"]), spec["kind"], case))
    assert without != [], f"{fname}: removing {spec['rule']} changes nothing this case can see: the fixture proves nothing"


def test_every_rule_names_the_section_of_the_skill_it_comes_from():
    sections = {line.split(".")[0].strip("# ").strip() for line in SKILL.read_text().splitlines() if line.startswith("## ")}
    assert {r["section"] for r in RULES.rules} <= sections  # "## 1. Is this file the model at all?" -> "1"
    assert {r["section"] for r in RULES.rules} == {"1", "2", "3", "4", "5", "6", "7"}


def test_a_rule_the_engine_cannot_read_is_an_error_not_a_silent_skip():
    bad = json.loads(json.dumps(RULES.data))
    bad["rules"][0]["when"] = {"fact": "file.name", "op": "sounds_like", "value": "x"}
    with pytest.raises(R.RulesError, match="unknown operator"):
        R.Rules(bad)
    bad = json.loads(json.dumps(RULES.data))
    bad["rules"][0]["effects"].append({"action": "teleport"})
    with pytest.raises(R.RulesError, match="unknown action"):
        R.Rules(bad)


def test_a_missing_fact_never_satisfies_a_test_that_needs_it():
    f = {"a": 5, "s": "Hello", "l": ["x", "Y"]}
    t = lambda **p: R.holds({"fact": p.pop("fact"), **p}, f)  # noqa: E731
    assert t(fact="a", op="gt", value=4) and not t(fact="a", op="gt", value=5) and t(fact="a", op="ge", value=5)
    assert not t(fact="nope", op="gt", value=0) and not t(fact="nope", op="eq", value=None) and not t(fact="nope", op="ne", value=1)
    assert t(fact="nope", op="missing") and t(fact="a", op="exists") and not t(fact="nope", op="exists")
    assert t(fact="s", op="eq", value="hello") and t(fact="s", op="contains", value="ELL") and t(fact="l", op="contains", value="y") and not t(fact="l", op="contains", value="z")
    assert t(fact="nope", op="not_matches", value="x") and t(fact="s", op="not_matches", value="^h$") and not t(fact="s", op="not_matches", value="(?i)^h")
    assert t(fact="nope", op="not_in", value=[1]) and not t(fact="a", op="not_in", value=[5]) and not t(fact="nope", op="in", value=[1])
    assert t(fact="s", op="differs", other_fact="l") and not t(fact="nope", op="differs", other_fact="a") and not t(fact="a", op="differs", other_fact="nope")


def test_the_first_kind_wins_and_a_rule_can_skip_files_of_a_kind_already_decided():
    f = {**__import__("catalog_rules_support").BASE_FILE, "gguf.general.architecture": "clip", "gguf.general.type": "mmproj", "gguf.has_chat_template": False, "hub.pipeline_tag": "image-text-to-text"}
    row = R.derive_row(f, RULES)
    assert row["kind"] == "vision-projector" and "R4.1-chat-template" not in row["rules_fired"] and "R4.2-vision" not in row["rules_fired"]


def test_a_derived_row_keeps_what_was_declared_next_to_what_was_verified():
    from catalog_rules_support import BASE_FILE
    row = R.derive_row({**BASE_FILE, "file.declared_params_b": 5.0, "gguf.param_count": 494_032_768}, RULES)
    assert row["params_b"] == 0.49
    c = next(x for x in row["conflicts"] if x["field"] == "params_b")
    assert (c["declared"], c["verified"], c["winner"]) == (5.0, 0.49, "verified") and "verified-by-code" in c["label"]


def test_a_clean_verified_model_is_offered_by_default_with_nothing_excluded():
    from catalog_rules_support import BASE_FILE
    row = R.derive_row(BASE_FILE, RULES)
    assert row["offer_by_default"] is True and row["excluded"] == [] and row["kind"] == "chat" and row["requires_task"] == {} and row["labels"] == [] and row["license_verified"] is True


def test_the_shipped_lockfile_was_checked_with_the_rules_that_are_shipped():
    """A change to rules.json without re-running scripts/catalog_verify.py leaves the lock describing a different rulebook."""
    import yaml
    from envrec.catalog.verify import rules_digest
    lock = yaml.safe_load((Path(__file__).resolve().parent.parent / "catalog.lock.yaml").read_text())
    assert lock["verified"]["rules"] == rules_digest(RULES)[:16], "rules.json changed: run `python scripts/catalog_verify.py --write` and review the diff"
